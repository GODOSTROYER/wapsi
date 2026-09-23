"""Edge-aware GraphSAGE for mule scoring, in pure PyTorch (no torch_geometric / DGL: PyG has no supported ROCm path).

Message passing uses only ``index_add_`` and ``scatter_reduce(reduce="amax")``, both standard ATen ops with HIP
kernels in ROCm PyTorch, so the same code runs on CPU, CUDA and AMD Instinct (MI300X) through the ``torch.cuda``
API. No custom kernels, no CUDA-only ops.

Layer (``EdgeSageLayer``), for node v with hidden state h_v and encoded edge features e:

    in-messages   m_{u->v} = ReLU(W_in  [h_u || e_uv])   aggregated over in-edges  with mean and max
    out-messages  m_{v->w} = ReLU(W_out [h_w || e_vw])   aggregated over out-edges with mean and max
    h_v' = LayerNorm(h_v + Dropout(ReLU(W_upd [h_v || mean_in || max_in || mean_out || max_out])))

In- and out-neighbourhoods are kept separate because direction carries the signal (who paid whom); every
transaction is its own edge (multigraph). Head: MLP on [h_L || h_0] -> one logit (mule vs legitimate profile).

Temporal variant (``ModelConfig.temporal``): a learnable Time2Vec encoding of each edge's time relative to the case
anchor is concatenated to the edge features before the edge encoder (TGAT-style time encoding).

Node / edge standardisation (mean / std from the training graphs) lives in model buffers, so a checkpoint is
self-contained.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import torch
from torch import nn

CHECKPOINT_FORMAT = "wapsi-gnn-checkpoint/1"


# --------------------------------------------------------------------------- device


def resolve_device(spec: str = "auto") -> torch.device:
    """``auto`` -> ``cuda`` when available (ROCm builds expose HIP devices through ``torch.cuda``), else ``cpu``."""
    spec = (spec or "auto").lower()
    if spec == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if spec.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(
            "--device cuda requested but torch.cuda.is_available() is False (no CUDA/ROCm device)"
        )
    return torch.device(spec)


DEFAULT_CPU_THREADS = 4


def configure_cpu_threads(device: torch.device, threads: int | None = None) -> int:
    """Case subgraphs are small: many intra-op threads only add contention (measured ~1000x slower steps with 16
    threads on a busy 22-core Windows dev box). CPU runs default to ``DEFAULT_CPU_THREADS``; GPU runs are untouched."""
    if threads:
        torch.set_num_threads(int(threads))
    elif device.type == "cpu" and torch.get_num_threads() > DEFAULT_CPU_THREADS:
        torch.set_num_threads(DEFAULT_CPU_THREADS)
    return torch.get_num_threads()


def device_info(device: torch.device) -> dict:
    info = {
        "device": str(device),
        "torch": torch.__version__,
        "backend": "cpu",
        "hip": getattr(torch.version, "hip", None),
        "cuda": getattr(torch.version, "cuda", None),
        "threads": torch.get_num_threads(),
    }
    if device.type == "cuda":
        info["backend"] = "rocm" if info["hip"] else "cuda"
        info["name"] = torch.cuda.get_device_name(device)
        props = torch.cuda.get_device_properties(device)
        info["total_memory_gb"] = round(props.total_memory / 2**30, 1)
    return info


# --------------------------------------------------------------------------- scatter helpers


def scatter_sum(src: torch.Tensor, index: torch.Tensor, n: int) -> torch.Tensor:
    out = src.new_zeros((n, *src.shape[1:]))
    return out.index_add(0, index, src)


def scatter_mean(src: torch.Tensor, index: torch.Tensor, n: int) -> torch.Tensor:
    total = scatter_sum(src, index, n)
    count = src.new_zeros(n).index_add(0, index, src.new_ones(index.shape[0])).clamp_(min=1.0)
    return total / count.unsqueeze(-1)


def scatter_max(src: torch.Tensor, index: torch.Tensor, n: int) -> torch.Tensor:
    """Per-node max; nodes without edges get 0 (messages are post-ReLU, so 0 is the neutral element)."""
    out = src.new_zeros((n, src.shape[1]))
    if index.numel() == 0:
        return out
    return out.scatter_reduce(0, index.unsqueeze(-1).expand_as(src), src, reduce="amax", include_self=False)


# --------------------------------------------------------------------------- modules


@dataclass
class ModelConfig:
    node_dim: int
    edge_dim: int
    hidden: int = 64
    edge_hidden: int = 32
    layers: int = 2
    dropout: float = 0.1
    temporal: bool = False
    time_dim: int = 8

    @classmethod
    def from_dict(cls, d: dict) -> ModelConfig:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class Time2Vec(nn.Module):
    """t (hours) -> [w0 t + b0, sin(W t + B)] with t in days."""

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.linear = nn.Linear(1, 1)
        self.periodic = nn.Linear(1, max(dim - 1, 1))

    def forward(self, t_hours: torch.Tensor) -> torch.Tensor:
        t = (t_hours / 24.0).unsqueeze(-1)
        return torch.cat([self.linear(t), torch.sin(self.periodic(t))], dim=-1)


class EdgeSageLayer(nn.Module):
    def __init__(self, hidden: int, edge_hidden: int, dropout: float) -> None:
        super().__init__()
        self.msg_in = nn.Linear(hidden + edge_hidden, hidden)
        self.msg_out = nn.Linear(hidden + edge_hidden, hidden)
        self.update = nn.Linear(5 * hidden, hidden)
        self.norm = nn.LayerNorm(hidden)
        self.dropout = nn.Dropout(dropout)

    def forward(self, h: torch.Tensor, edge_index: torch.Tensor, e: torch.Tensor) -> torch.Tensor:
        n = h.shape[0]
        src, dst = edge_index[0], edge_index[1]
        m_in = torch.relu(self.msg_in(torch.cat([h[src], e], dim=-1)))  # u -> v, lands on dst
        m_out = torch.relu(self.msg_out(torch.cat([h[dst], e], dim=-1)))  # v -> w, lands on src
        agg = torch.cat(
            [
                h,
                scatter_mean(m_in, dst, n),
                scatter_max(m_in, dst, n),
                scatter_mean(m_out, src, n),
                scatter_max(m_out, src, n),
            ],
            dim=-1,
        )
        return self.norm(h + self.dropout(torch.relu(self.update(agg))))


class WapsiGnn(nn.Module):
    def __init__(self, cfg: ModelConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.register_buffer("x_mean", torch.zeros(cfg.node_dim))
        self.register_buffer("x_std", torch.ones(cfg.node_dim))
        self.register_buffer("e_mean", torch.zeros(cfg.edge_dim))
        self.register_buffer("e_std", torch.ones(cfg.edge_dim))
        self.node_in = nn.Linear(cfg.node_dim, cfg.hidden)
        self.time_enc = Time2Vec(cfg.time_dim) if cfg.temporal else None
        self.edge_in = nn.Linear(cfg.edge_dim + (cfg.time_dim if cfg.temporal else 0), cfg.edge_hidden)
        self.layers = nn.ModuleList(
            EdgeSageLayer(cfg.hidden, cfg.edge_hidden, cfg.dropout) for _ in range(cfg.layers)
        )
        self.dropout = nn.Dropout(cfg.dropout)
        self.head = nn.Sequential(
            nn.Linear(2 * cfg.hidden, cfg.hidden),
            nn.ReLU(),
            nn.Dropout(cfg.dropout),
            nn.Linear(cfg.hidden, 1),
        )

    # -- normalisation
    @torch.no_grad()
    def set_normalization(self, x_mean, x_std, e_mean, e_std) -> None:
        for name, v in (("x_mean", x_mean), ("x_std", x_std), ("e_mean", e_mean), ("e_std", e_std)):
            t = torch.as_tensor(v, dtype=torch.float32)
            if name.endswith("std"):
                t = torch.where(t > 1e-6, t, torch.ones_like(t))
            getattr(self, name).copy_(t)

    def normalize_x(self, x: torch.Tensor) -> torch.Tensor:
        return (x - self.x_mean) / self.x_std

    def encode_edges(self, edge_attr: torch.Tensor, edge_time: torch.Tensor | None) -> torch.Tensor:
        e = (edge_attr - self.e_mean) / self.e_std
        if self.time_enc is not None:
            t = edge_time if edge_time is not None else edge_attr.new_zeros(edge_attr.shape[0])
            e = torch.cat([e, self.time_enc(t)], dim=-1)
        return torch.relu(self.edge_in(e))

    # -- forward
    def embed(
        self,
        xn: torch.Tensor,
        edge_index: torch.Tensor,
        edge_attr: torch.Tensor,
        edge_time: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Standardised node features -> (h_0, h_L)."""
        h0 = torch.relu(self.node_in(xn))
        e = self.encode_edges(edge_attr, edge_time)
        h = self.dropout(h0)
        for layer in self.layers:
            h = layer(h, edge_index, e)
        return h0, h

    def forward_normalized(self, xn, edge_index, edge_attr, edge_time=None) -> torch.Tensor:
        h0, h = self.embed(xn, edge_index, edge_attr, edge_time)
        return self.head(torch.cat([h, h0], dim=-1)).squeeze(-1)

    def forward(self, x, edge_index, edge_attr, edge_time=None) -> torch.Tensor:
        """Raw (log-transformed) node features -> mule logits [N]."""
        return self.forward_normalized(self.normalize_x(x), edge_index, edge_attr, edge_time)


class EdgeHead(nn.Module):
    """Edge-level classifier (AMLworld pre-training): [h_src || h_dst || e] -> laundering logit."""

    def __init__(self, hidden: int, edge_hidden: int) -> None:
        super().__init__()
        self.mlp = nn.Sequential(nn.Linear(2 * hidden + edge_hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1))

    def forward(self, h: torch.Tensor, edge_index: torch.Tensor, e: torch.Tensor) -> torch.Tensor:
        return self.mlp(torch.cat([h[edge_index[0]], h[edge_index[1]], e], dim=-1)).squeeze(-1)


TRANSFERABLE_PREFIXES = ("layers.", "edge_in.", "time_enc.")


def transfer_weights(src: nn.Module, dst: nn.Module) -> list[str]:
    """Copy message-passing / edge-encoder weights whose shapes match (node input encoder is re-initialised)."""
    s, d = src.state_dict(), dst.state_dict()
    moved = [
        k for k, v in s.items() if k.startswith(TRANSFERABLE_PREFIXES) and k in d and d[k].shape == v.shape
    ]
    dst.load_state_dict({**d, **{k: s[k] for k in moved}})
    return moved


# --------------------------------------------------------------------------- checkpoints


def save_checkpoint(model: WapsiGnn, out_dir: str | Path, extra: dict | None = None) -> Path:
    """``model.safetensors`` (weights + normalisation buffers) + ``config.json``. No pickle anywhere."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    state = {k: v.detach().to("cpu").contiguous() for k, v in model.state_dict().items()}
    try:
        from safetensors.torch import save_file

        save_file(state, str(out / "model.safetensors"))
        weights = "model.safetensors"
    except ImportError:  # pragma: no cover - safetensors ships with the amd extra
        torch.save(state, out / "model.pt")
        weights = "model.pt"
    cfg = {"format": CHECKPOINT_FORMAT, "weights": weights, "model": asdict(model.cfg), **(extra or {})}
    (out / "config.json").write_text(json.dumps(cfg, indent=2, default=str), encoding="utf-8")
    return out


def load_checkpoint(path: str | Path, device: torch.device | str = "cpu") -> tuple[WapsiGnn, dict]:
    path = Path(path)
    if path.is_file():
        path = path.parent
    cfg = json.loads((path / "config.json").read_text(encoding="utf-8"))
    if cfg.get("format") != CHECKPOINT_FORMAT:
        raise ValueError(f"{path} is not a {CHECKPOINT_FORMAT} checkpoint (format={cfg.get('format')!r})")
    model = WapsiGnn(ModelConfig.from_dict(cfg["model"]))
    weights = path / cfg.get("weights", "model.safetensors")
    if weights.suffix == ".safetensors":
        from safetensors.torch import load_file

        state = load_file(str(weights), device="cpu")
    else:
        state = torch.load(weights, map_location="cpu", weights_only=True)
    model.load_state_dict(state)
    model.to(device).eval()
    return model, cfg


def count_parameters(model: nn.Module) -> int:
    return int(sum(p.numel() for p in model.parameters() if p.requires_grad))
