"""Background population: individuals, employers/suppliers, merchants, billers and nodal/pool/escrow accounts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from wapsi.synth import names as nm
from wapsi.synth.builder import DAY, WorldBuilder

BANK_SHARE = {"HB01": 0.5, "OB02": 0.28, "OB03": 0.22}


@dataclass
class Population:
    individuals: np.ndarray
    businesses: np.ndarray
    merchants: np.ndarray
    merchant_cat: dict[str, np.ndarray]
    billers: np.ndarray
    pools: np.ndarray
    wealth: np.ndarray  # per account index (all background accounts), rupees scale for amounts
    extra: dict = field(default_factory=dict)


def _bank(rng: np.random.Generator, home: str, share_home: float | None = None) -> str:
    keys = list(BANK_SHARE)
    p = np.array([BANK_SHARE[k] for k in keys])
    if share_home is not None:
        p = np.array([share_home, (1 - share_home) * 0.56, (1 - share_home) * 0.44])
    b = keys[int(rng.choice(3, p=p / p.sum()))]
    return home if b == "HB01" else b


def _opened(b: WorldBuilder, rng: np.random.Generator, recent_p: float = 0.02) -> int:
    if rng.random() < recent_p:  # opened during the world (noise: new but legitimate)
        return (
            b.start_s
            + int(rng.integers(0, max(1, b.days - 5))) * DAY
            + int(rng.integers(10 * 3600, 17 * 3600))
        )
    years = rng.uniform(0.3, 20.0)
    return b.start_s - int(years * 365 * DAY) + int(rng.integers(10 * 3600, 17 * 3600))


def build_population(b: WorldBuilder, n_background: int, n_pools: int) -> Population:
    rng = b.rng
    n_biz = max(8, int(n_background * 0.04))
    n_merch = max(20, int(n_background * 0.07))
    n_billers = len(nm.BILLERS)
    n_ind = n_background - n_biz - n_merch - n_billers - n_pools

    individuals, businesses, merchants, billers, pools = [], [], [], [], []
    merchant_cat: dict[str, list[int]] = {k: [] for k in nm.MERCHANT_CATEGORIES}

    for _ in range(n_ind):
        name, city, _region = b.person_name()
        cid = b.new_customer(name, city, b.background_kyc(), b.random_dob())
        individuals.append(
            b.new_account(_bank(rng, b.home), "INDIVIDUAL", _opened(b, rng), cid, name=name, city=city)
        )

    for _ in range(n_biz):
        bname = b.business_name()
        _, city, _ = b.person_name()
        opened = _opened(b, rng, recent_p=0.0)
        gstin = None
        if rng.random() < 0.85:
            status = "ACTIVE" if rng.random() > 0.03 else "CANCELLED"
            reg = b.ist_date(opened) - timedelta(days=int(rng.integers(0, 200)))
            gstin = b.new_gstin(bname, status, max(reg, date(2017, 7, 1)))
        cid = b.new_customer(bname, city, "FULL", None)
        businesses.append(
            b.new_account(_bank(rng, b.home), "BUSINESS", opened, cid, name=bname, city=city, gstin=gstin)
        )

    cats = list(nm.MERCHANT_CATEGORY_WEIGHTS)
    cat_p = np.array([nm.MERCHANT_CATEGORY_WEIGHTS[c] for c in cats])
    for i in range(n_merch):
        # guarantee a few home-bank merchants in every category (rings need them)
        if i < 3 * len(cats):
            cat, bank = cats[i % len(cats)], b.home
        else:
            cat, bank = cats[int(rng.choice(len(cats), p=cat_p / cat_p.sum()))], _bank(rng, b.home, 0.55)
        mname = b.merchant_name(cat)
        _, city, _ = b.person_name()
        opened = _opened(b, rng, recent_p=0.01)
        gstin = None
        if rng.random() < 0.9 or cat in nm.WASH_CATEGORIES:
            u = rng.random()
            status = "ACTIVE" if u > 0.05 else ("CANCELLED" if u > 0.01 else "SUSPENDED")
            if cat in nm.WASH_CATEGORIES and i < 3 * len(cats):
                status = "ACTIVE"
            reg = b.ist_date(opened) - timedelta(days=int(rng.integers(0, 400)))
            gstin = b.new_gstin(
                mname, status, max(reg, date(2017, 7, 1)), entity=str(rng.choice(["F", "P", "C"]))
            )
        cid = b.new_customer(mname, city, "FULL", None)
        idx = b.new_account(
            bank, "MERCHANT", opened, cid, name=mname, city=city, gstin=gstin, category=cat, role="MERCHANT"
        )
        merchants.append(idx)
        merchant_cat[cat].append(idx)

    for i, biller in enumerate(nm.BILLERS):
        bname = f"{nm.BIZ_STEMS[i * 3 % len(nm.BIZ_STEMS)]} {biller}"
        gstin = b.new_gstin(bname, "ACTIVE", date(2017, 7, 1))
        cid = b.new_customer(bname, "Mumbai", "FULL", None)
        billers.append(
            b.new_account(
                b.home if i % 2 == 0 else ("OB02" if i % 4 == 1 else "OB03"),
                "MERCHANT",
                b.start_s - 3000 * DAY,
                cid,
                name=bname,
                gstin=gstin,
                category="utility",
                role="MERCHANT",
            )
        )

    kinds = ["POOL", "NODAL", "ESCROW"]
    for i in range(n_pools):
        bank = b.home if i % 3 != 2 else ("OB02" if i % 2 == 0 else "OB03")
        pname = nm.POOL_NAMES[i % len(nm.POOL_NAMES)] + (
            f" {i // len(nm.POOL_NAMES) + 1}" if i >= len(nm.POOL_NAMES) else ""
        )
        opening = float(np.round(rng.uniform(2e7, 2e8), -3))  # 2-20 crore
        pools.append(
            b.new_account(
                bank,
                kinds[i % 3],
                b.start_s - int(rng.integers(400, 3000)) * DAY,
                None,
                name=pname,
                opening=opening,
                role="INTERMEDIARY",
            )
        )

    n_total = len(b.acct_ids)
    wealth = np.zeros(n_total)
    ind = np.array(individuals, dtype=np.int64)
    wealth[ind] = np.exp(rng.normal(np.log(30_000), 0.9, ind.size))
    wealth[np.array(businesses)] = np.exp(rng.normal(np.log(3_000_000), 0.8, len(businesses)))
    wealth[np.array(merchants)] = np.exp(rng.normal(np.log(250_000), 0.8, len(merchants)))
    wealth[np.array(billers)] = 5e7
    wealth[np.array(pools)] = 1e8
    return Population(
        individuals=ind,
        businesses=np.array(businesses, dtype=np.int64),
        merchants=np.array(merchants, dtype=np.int64),
        merchant_cat={k: np.array(v, dtype=np.int64) for k, v in merchant_cat.items()},
        billers=np.array(billers, dtype=np.int64),
        pools=np.array(pools, dtype=np.int64),
        wealth=wealth,
    )
