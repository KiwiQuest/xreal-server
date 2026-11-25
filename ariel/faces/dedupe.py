"""Porovnani databaze same se sebou: hleda osoby, ktere jsou v ni vicekrat.

Nic se nerozhoduje automaticky. Podezrele pary se spoji do skupin (union-find)
a skupina dostane stav "pending" = ceka na kontrolu clovekem.
Par, ktery clovek oznacil jako "different", se uz znovu neoznacuje.
"""
import secrets

import numpy as np

from .vault import Vault, iso, now, pair_key


def similar_pairs(vault: Vault, only_ids=None):
    """Vrati [(a, b, podobnost, uroven)] pro pary nad prahem 'maybe', bez paru zamitnutych clovekem."""
    thr, maybe = vault.cfg["threshold"], vault.cfg["maybe"]
    ids, E = vault.matrix()
    n = len(ids)
    if n < 2:
        return []
    S = E @ E.T
    out = []
    rows = range(n) if only_ids is None else [i for i, x in enumerate(ids) if x in set(only_ids)]
    seen = set()
    for i in rows:
        for j in np.nonzero(S[i] >= maybe)[0]:
            j = int(j)
            if j == i:
                continue
            k = pair_key(ids[i], ids[j])
            if k in seen:
                continue
            seen.add(k)
            d = vault.data["decisions"].get(k)
            if d and d["v"] == "different":
                continue
            s = float(S[i, j])
            out.append((ids[i], ids[j], round(s, 3), "likely" if s >= thr else "maybe"))
    out.sort(key=lambda p: -p[2])
    return out


def _groups(pairs):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, *_ in pairs:
        parent[find(a)] = find(b)
    groups = {}
    for a, b, *_ in pairs:
        groups.setdefault(find(a), set()).update((a, b))
    return list(groups.values())


def run(vault: Vault, only_ids=None):
    """Prepocita skupiny ke kontrole. Rozhodnute skupiny ("same") zachova. Vraci pocet cekajicich skupin."""
    pairs = similar_pairs(vault, only_ids)
    if only_ids is not None:
        # inkrementalne: pridat k existujicim parum z cekajicich skupin
        for rv in vault.data["reviews"]:
            if rv["status"] == "pending":
                pairs += [tuple(p) for p in rv["pairs"]]
    pending_old = {frozenset(rv["members"]): rv for rv in vault.data["reviews"] if rv["status"] == "pending"}
    kept = [rv for rv in vault.data["reviews"] if rv["status"] != "pending"]
    confirmed = set()
    for rv in kept:
        for a in rv["members"]:
            for b in rv["members"]:
                if a < b:
                    confirmed.add(pair_key(a, b))
    pairs = [p for p in pairs if pair_key(p[0], p[1]) not in confirmed]
    # odstranit duplicitni pary
    uniq = {}
    for p in pairs:
        uniq[pair_key(p[0], p[1])] = p
    pairs = list(uniq.values())

    new = []
    for members in _groups(pairs):
        ps = [list(p) for p in pairs if p[0] in members and p[1] in members]
        old = pending_old.get(frozenset(members))
        new.append({
            "id": old["id"] if old else "R" + secrets.token_hex(3),
            "members": sorted(members),
            "pairs": sorted(ps, key=lambda p: -p[2]),
            "level": "likely" if any(p[3] == "likely" for p in ps) else "maybe",
            "status": "pending",
            "t": old["t"] if old else iso(now()),
        })
    new.sort(key=lambda rv: -rv["pairs"][0][2])
    vault.data["reviews"] = kept + new
    return len(new)


def pending(vault: Vault):
    return [rv for rv in vault.data["reviews"] if rv["status"] == "pending"]


def flagged_ids(vault: Vault):
    return {m for rv in pending(vault) for m in rv["members"]}


def resolve(vault: Vault, review_id, verdict, keep=None):
    """verdict: 'same' (stejna osoba; volitelne keep = ID, ktere zustane, ostatni se smazou)
                'different' (ruzne osoby; pary se uz neoznacuji)"""
    rv = next((r for r in vault.data["reviews"] if r["id"] == review_id), None)
    if rv is None:
        raise KeyError(review_id)
    for a, b, *_ in rv["pairs"]:
        vault.decide(a, b, verdict)
    if verdict == "different":
        vault.data["reviews"].remove(rv)
        return 0
    rv["status"] = "same"
    rv["decided"] = iso(now())
    if keep:
        drop = [m for m in rv["members"] if m != keep]
        return vault.delete(drop, reason=f"duplicate_of:{keep}")
    return 0
