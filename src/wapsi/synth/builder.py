"""Mutable world state shared by the population, ring and background generators.

Internal representation: accounts are integer indices into Python lists (<= ~60k), transactions are numpy
chunks of integer codes (from/to account index, -1 for None) so the 2M-row world stays vectorised.
Time is int64 epoch seconds (UTC) until the final assembly.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import numpy as np

from wapsi.models import Channel
from wapsi.synth import names as nm

MINUTE = 60
HOUR = 3600
DAY = 86400

CHANNELS: list[str] = [c.value for c in Channel]
CH = {c: i for i, c in enumerate(CHANNELS)}

UPI_LIMIT = 100_000.0
IMPS_LIMIT = 500_000.0


def epoch_s(ts: datetime) -> int:
    return int(ts.timestamp())


def from_epoch_s(s: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=int(s))


class StringTable:
    """Interns strings to int codes (deterministic insertion order)."""

    def __init__(self) -> None:
        self.values: list[str] = []
        self._idx: dict[str, int] = {}

    def code(self, s: str) -> int:
        c = self._idx.get(s)
        if c is None:
            c = len(self.values)
            self._idx[s] = c
            self.values.append(s)
        return c

    def codes(self, items: list[str]) -> np.ndarray:
        return np.fromiter((self.code(s) for s in items), dtype=np.int32, count=len(items))


@dataclass
class TxnChunk:
    ts: np.ndarray  # int64 epoch seconds
    frm: np.ndarray  # int32 account index, -1 = None
    to: np.ndarray  # int32 account index, -1 = None
    amt: np.ndarray  # float64 rupees (2 dp)
    chan: np.ndarray  # int8 index into CHANNELS
    remark: np.ndarray  # int32 code into WorldBuilder.text
    label: np.ndarray  # int32 code into WorldBuilder.text, -1 = None

    def __len__(self) -> int:
        return int(self.ts.shape[0])

    @staticmethod
    def concat(chunks: list[TxnChunk]) -> TxnChunk:
        return TxnChunk(
            *(
                np.concatenate([getattr(c, f) for c in chunks])
                for f in ("ts", "frm", "to", "amt", "chan", "remark", "label")
            )
        )


def upi_or_imps(amount: float, rng: np.random.Generator, p_upi: float = 0.6) -> int:
    if amount <= UPI_LIMIT and rng.random() < p_upi:
        return CH["UPI"]
    if amount <= IMPS_LIMIT:
        return CH["IMPS"]
    return CH["RTGS"] if amount >= 200_000 and rng.random() < 0.6 else CH["NEFT"]


@dataclass
class WorldBuilder:
    name: str
    seed: int
    start_s: int  # world start (IST midnight) as epoch seconds
    days: int
    home: str
    rng: np.random.Generator

    # accounts
    acct_ids: list[str] = field(default_factory=list)
    acct_bank: list[str] = field(default_factory=list)
    acct_cust: list[str | None] = field(default_factory=list)
    acct_type: list[str] = field(default_factory=list)
    acct_opened: list[int] = field(default_factory=list)
    acct_gstin: list[str | None] = field(default_factory=list)
    acct_status: list[str] = field(default_factory=list)
    acct_opening: list[float] = field(default_factory=list)
    acct_city: list[str] = field(default_factory=list)
    acct_category: list[str | None] = field(default_factory=list)
    acct_name: list[str] = field(default_factory=list)
    dedicated: list[bool] = field(default_factory=list)  # ring-controlled: no background traffic
    role: list[str] = field(default_factory=list)
    ring: list[str | None] = field(default_factory=list)
    typology: list[str | None] = field(default_factory=list)

    customers: list[dict] = field(default_factory=list)
    gst: list[dict] = field(default_factory=list)
    devices: list[tuple[int, int, int]] = field(default_factory=list)  # (device_no, acct_idx, first_seen_s)
    signal_override: dict[int, tuple[str, float, int]] = field(default_factory=dict)

    text: StringTable = field(default_factory=StringTable)
    chunks: list[TxnChunk] = field(default_factory=list)
    # invoices: dict rows with "txn" = global txn index (resolved after assembly)
    invoices: list[dict] = field(default_factory=list)
    complaints: list[dict] = field(default_factory=list)
    complaint_txns: list[tuple[str, int]] = field(default_factory=list)

    _acct_pool: dict[str, list[int]] = field(default_factory=dict)
    _cust_no: int = 0
    _dev_no: int = 0
    _gst_no: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.text.code("")

    # ------------------------------------------------------------------ time
    @property
    def end_s(self) -> int:
        return self.start_s + self.days * DAY

    def day_start(self, d: int) -> int:
        return self.start_s + d * DAY

    def ist_date(self, s: int) -> date:
        return (from_epoch_s(s) + timedelta(hours=5, minutes=30)).date()

    # ------------------------------------------------------------------ ids
    def reserve_account_numbers(self, per_bank: dict[str, int]) -> None:
        for bank, n in per_bank.items():
            nums = self.rng.choice(8_999_999_999, size=n, replace=False) + 1_000_000_000
            self._acct_pool[bank] = nums.tolist()[::-1]

    def _next_account_id(self, bank: str) -> str:
        pool = self._acct_pool[bank]
        if not pool:
            raise RuntimeError(f"account number pool exhausted for {bank}")
        return f"{bank}-{pool.pop()}"

    def mobile(self) -> str:
        r = self.rng
        return f"{int(r.integers(6, 10))}{int(r.integers(0, 10))}XXXXXX{int(r.integers(0, 100)):02d}"

    def pan_masked(self) -> str:
        return (
            f"XXXXX{int(self.rng.integers(0, 10000)):04d}{string.ascii_uppercase[int(self.rng.integers(26))]}"
        )

    def new_gstin(self, legal_name: str, status: str, registered: date, entity: str = "C") -> str:
        r = self.rng
        letters = string.ascii_uppercase
        while True:
            state = int(r.choice([27, 24, 29, 33, 36, 7, 9, 19, 32, 8, 23, 3, 21, 18]))
            pan = (
                "".join(letters[int(i)] for i in r.integers(0, 26, 3))
                + entity
                + letters[int(r.integers(26))]
                + f"{int(r.integers(0, 10000)):04d}"
                + letters[int(r.integers(26))]
            )
            chk = (string.digits + letters)[int(r.integers(36))]
            g = f"{state:02d}{pan}{int(r.integers(1, 4))}Z{chk}"
            if g not in self._gst_no:
                self._gst_no.add(g)
                break
        self.gst.append({"gstin": g, "legal_name": legal_name, "status": status, "registered_at": registered})
        return g

    # ------------------------------------------------------------------ people
    def person_name(self, region: str | None = None) -> tuple[str, str, str]:
        r = self.rng
        if region is None:
            keys = list(nm.REGION_WEIGHTS)
            p = np.array([nm.REGION_WEIGHTS[k] for k in keys])
            region = keys[int(r.choice(len(keys), p=p / p.sum()))]
        given, surnames, cities = nm.REGIONS[region]
        name = f"{given[int(r.integers(len(given)))]} {surnames[int(r.integers(len(surnames)))]}"
        return name, cities[int(r.integers(len(cities)))], region

    def business_name(self) -> str:
        r = self.rng
        return (
            f"{nm.BIZ_STEMS[int(r.integers(len(nm.BIZ_STEMS)))]} "
            f"{nm.BIZ_TRADES[int(r.integers(len(nm.BIZ_TRADES)))]} "
            f"{nm.BIZ_SUFFIX[int(r.integers(len(nm.BIZ_SUFFIX)))]}"
        )

    def merchant_name(self, category: str) -> str:
        r = self.rng
        nouns = nm.MERCHANT_CATEGORIES[category][0]
        return f"{nm.MERCHANT_PREFIX[int(r.integers(len(nm.MERCHANT_PREFIX)))]} {nouns[int(r.integers(len(nouns)))]}"

    def new_customer(self, name: str, city: str, kyc: str, dob: date | None) -> str:
        self._cust_no += 1
        cid = f"CUST-{self._cust_no:07d}"
        self.customers.append(
            {
                "customer_id": cid,
                "name": name,
                "mobile": self.mobile(),
                "pan_masked": self.pan_masked(),
                "kyc_tier": kyc,
                "dob": dob,
                "city": city,
            }
        )
        return cid

    def random_dob(self, lo_year: int = 1952, hi_year: int = 2005) -> date:
        return date(int(self.rng.integers(lo_year, hi_year + 1)), 1, 1) + timedelta(
            days=int(self.rng.integers(365))
        )

    def background_kyc(self) -> str:
        u = self.rng.random()
        return "FULL" if u < 0.86 else ("VKYC" if u < 0.96 else "MIN")

    # ------------------------------------------------------------------ accounts
    def new_account(
        self,
        bank: str,
        acct_type: str,
        opened_s: int,
        cust_id: str | None,
        *,
        name: str = "",
        gstin: str | None = None,
        status: str = "ACTIVE",
        opening: float = 0.0,
        city: str = "",
        category: str | None = None,
        role: str = "LEGIT",
        dedicated: bool = False,
        ring: str | None = None,
        typology: str | None = None,
    ) -> int:
        idx = len(self.acct_ids)
        self.acct_ids.append(self._next_account_id(bank))
        self.acct_bank.append(bank)
        self.acct_cust.append(cust_id)
        self.acct_type.append(acct_type)
        self.acct_opened.append(int(opened_s))
        self.acct_gstin.append(gstin)
        self.acct_status.append(status)
        self.acct_opening.append(round(float(opening), 2))
        self.acct_city.append(city)
        self.acct_category.append(category)
        self.acct_name.append(name)
        self.dedicated.append(dedicated)
        self.role.append(role)
        self.ring.append(ring)
        self.typology.append(typology)
        return idx

    def add_device(self, acct: int, first_seen_s: int, device: int | None = None) -> int:
        if device is None:
            self._dev_no += 1
            device = self._dev_no
        self.devices.append((device, acct, int(first_seen_s)))
        return device

    def new_device_no(self) -> int:
        self._dev_no += 1
        return self._dev_no

    def txn_count(self) -> int:
        return sum(len(c) for c in self.chunks)
