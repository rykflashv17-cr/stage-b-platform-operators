"""Mergeable global quantiles for ``meta/stats.json`` (REPORT STAT-01).

File-level quantiles cannot be merged by averaging or by taking their
min/max. This module recomputes each quantile from the full published data
with two histogram passes over the parquet shards:

1. coarse: ``COARSE_BINS`` equal-width bins between the exact global min/max;
2. refine: ``REFINE_BINS`` sub-bins inside the coarse bin that holds each
   target order statistic, tracking the exact min/max of every sub-bin.

The result matches ``np.quantile(..., method="linear")`` exactly when the
sub-bin holding the order statistic contains a single distinct value, and is
otherwise bounded by that sub-bin's observed value span. The bound is
returned per feature so it can be published next to the statistics.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, Callable

import gc
from contextlib import contextmanager

import numpy as np

QUANTILES: tuple[tuple[float, str], ...] = (
    (0.01, "q01"),
    (0.10, "q10"),
    (0.50, "q50"),
    (0.90, "q90"),
    (0.99, "q99"),
)
COARSE_BINS = 4096
REFINE_BINS = 1024
METHOD = "two_pass_histogram_refinement_linear_interpolation"
# Files per worker task. A partial histogram is D x 4096 (coarse) / D x 10 x 1024 x 3 (refine)
# per feature (tens of MB for ~100 dims); one per file made the parent unpickle and add TBs
# for 10^5 files. Workers now sum a batch locally and return one partial per batch.
TASKS_PER_WORKER = 4


def _bin_index(values: np.ndarray, lo: np.ndarray, hi: np.ndarray, bins: int) -> np.ndarray:
    """Equal-width bin index of ``values`` [N,D] in [lo, hi] (broadcast), clipped."""
    width = hi - lo
    safe = np.where(width > 0, width, 1.0)
    scale = np.where(width > 0, bins / safe, 0.0)
    index = np.floor((values - lo) * scale)
    return np.clip(index, 0, bins - 1).astype(np.int64)


def coarse_histogram(
    values: np.ndarray, lo: np.ndarray, hi: np.ndarray, bins: int = COARSE_BINS
) -> np.ndarray:
    """Per-dimension counts [D, bins] for one file."""
    n, dims = values.shape
    index = _bin_index(values, lo, hi, bins) + np.arange(dims, dtype=np.int64) * bins
    return np.bincount(index.reshape(-1), minlength=dims * bins).reshape(dims, bins)


def refine_histogram(
    values: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    targets: np.ndarray,
    *,
    coarse_bins: int = COARSE_BINS,
    bins: int = REFINE_BINS,
) -> dict[str, np.ndarray]:
    """Sub-bin counts/min/max [D, R, bins] inside each target coarse bin [D, R]."""
    n, dims = values.shape
    ranks = targets.shape[1]
    counts = np.zeros((dims, ranks, bins), dtype=np.int64)
    minimum = np.full((dims, ranks, bins), np.inf, dtype=np.float64)
    maximum = np.full((dims, ranks, bins), -np.inf, dtype=np.float64)
    if n == 0:
        return {"count": counts, "min": minimum, "max": maximum}
    coarse = _bin_index(values, lo, hi, coarse_bins)
    width = (hi - lo) / coarse_bins
    for dim in range(dims):
        column = values[:, dim]
        for rank, target in enumerate(targets[dim]):
            selected = column[coarse[:, dim] == target]
            if selected.size == 0:
                continue
            edge_lo = lo[dim] + target * width[dim]
            edge_hi = edge_lo + width[dim]
            sub = _bin_index(selected, np.asarray(edge_lo), np.asarray(edge_hi), bins)
            counts[dim, rank] += np.bincount(sub, minlength=bins)
            np.minimum.at(minimum[dim, rank], sub, selected)
            np.maximum.at(maximum[dim, rank], sub, selected)
    return {"count": counts, "min": minimum, "max": maximum}


def _target_ranks(total: int) -> tuple[np.ndarray, np.ndarray]:
    """0-based order statistics (floor, ceil) and fractions for each quantile."""
    positions = np.asarray([q for q, _ in QUANTILES], dtype=np.float64) * (total - 1)
    floor = np.floor(positions).astype(np.int64)
    ceil = np.minimum(floor + 1, total - 1)
    return np.concatenate([floor, ceil]), positions - floor


def _locate(cumulative: np.ndarray, rank: int) -> tuple[int, int]:
    """Bin holding 0-based ``rank`` and the rank's offset inside that bin."""
    index = int(np.searchsorted(cumulative, rank, side="right"))
    before = int(cumulative[index - 1]) if index > 0 else 0
    return index, rank - before


def coarse_targets(histogram: np.ndarray, total: int) -> tuple[np.ndarray, np.ndarray]:
    """Coarse bin [D, R] and in-bin residual rank [D, R] for every target rank."""
    ranks, _ = _target_ranks(total)
    dims = histogram.shape[0]
    bins = np.zeros((dims, len(ranks)), dtype=np.int64)
    residual = np.zeros_like(bins)
    for dim in range(dims):
        cumulative = np.cumsum(histogram[dim])
        if int(cumulative[-1]) != total:
            raise ValueError("Coarse histogram count disagrees with the total count.")
        for r, rank in enumerate(ranks):
            bins[dim, r], residual[dim, r] = _locate(cumulative, int(rank))
    return bins, residual


def finalize_quantiles(
    refined: dict[str, np.ndarray], residual: np.ndarray, total: int
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Quantile values and absolute error bounds, keyed by ``q01`` ... ``q99``."""
    _, fractions = _target_ranks(total)
    dims, ranks, _ = refined["count"].shape
    order_values = np.zeros((dims, ranks), dtype=np.float64)
    order_bounds = np.zeros((dims, ranks), dtype=np.float64)
    for dim in range(dims):
        for r in range(ranks):
            cumulative = np.cumsum(refined["count"][dim, r])
            sub, offset = _locate(cumulative, int(residual[dim, r]))
            if sub >= len(cumulative):
                raise ValueError("Refined histogram lost the target order statistic.")
            low = refined["min"][dim, r, sub]
            high = refined["max"][dim, r, sub]
            members = int(refined["count"][dim, r, sub])
            if members > 1 and high > low:
                order_values[dim, r] = low + (high - low) * offset / (members - 1)
            else:
                order_values[dim, r] = low
            order_bounds[dim, r] = high - low
    half = len(QUANTILES)
    values: dict[str, np.ndarray] = {}
    bounds: dict[str, np.ndarray] = {}
    for i, (_, key) in enumerate(QUANTILES):
        lower, upper = order_values[:, i], order_values[:, i + half]
        values[key] = lower + (upper - lower) * fractions[i]
        bounds[key] = np.maximum(order_bounds[:, i], order_bounds[:, i + half])
    return values, bounds


@contextmanager
def frozen_gc():
    """Freeze the parent's objects before forking a process pool.

    Forked workers share the parent heap copy-on-write, but their garbage collector walks every
    inherited container and so dirties (copies) those pages: with the full EgoStandard merge
    (252k per-file stats in the parent, 96 workers) each worker copied gigabytes and rank 0 was
    OOM-killed (2026-10-13). ``gc.freeze`` moves existing objects out of the collector's reach.
    """
    gc.collect()
    gc.freeze()
    try:
        yield
    finally:
        gc.unfreeze()


def _batches(paths: list[str], num_workers: int) -> list[list[str]]:
    """Strided file batches (neighbouring files often differ in size, so stride balances)."""
    n = max(1, min(len(paths), max(1, int(num_workers)) * TASKS_PER_WORKER))
    return [paths[i::n] for i in range(n) if paths[i::n]]


def _run(
    jobs: list[dict[str, Any]], worker: Callable, num_workers: int, merge: Callable, desc: str
) -> None:
    """Run jobs with at most ``num_workers`` in flight; merge each result as it arrives."""
    total = len(jobs)
    if num_workers <= 1 or total <= 1:
        for job in jobs:
            merge(worker(job))
        return
    done = 0
    step = max(1, total // 10)
    with frozen_gc(), ProcessPoolExecutor(max_workers=min(num_workers, total)) as pool:
        pending = iter(jobs)
        inflight = set()
        for job in pending:
            inflight.add(pool.submit(worker, job))
            if len(inflight) >= num_workers:
                break
        while inflight:
            finished, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for future in finished:
                merge(future.result())
                done += 1
                if done % step == 0 or done == total:
                    print(f"[stats] quantile {desc} {done}/{total} batches", flush=True)
                job = next(pending, None)
                if job is not None:
                    inflight.add(pool.submit(worker, job))


def compute_global_quantiles(
    paths: list[str],
    load_columns: Callable[[str, list[str]], dict[str, np.ndarray]],
    extrema: dict[str, tuple[np.ndarray, np.ndarray, int]],
    *,
    num_workers: int = 1,
) -> tuple[dict[str, dict[str, np.ndarray]], dict[str, dict[str, Any]]]:
    """Global quantiles for every feature in ``extrema`` (name -> (min, max, count)).

    ``load_columns(path, names)`` must return name -> float64 [N, D] for one
    shard and be picklable (a module-level function).
    """
    names = sorted(extrema)
    histograms = {
        name: np.zeros((len(extrema[name][0]), COARSE_BINS), dtype=np.int64) for name in names
    }

    def merge_coarse(partial: dict[str, np.ndarray]) -> None:
        for name, histogram in partial.items():
            histograms[name] += histogram

    bounds = {name: (extrema[name][0], extrema[name][1]) for name in names}
    batches = _batches(list(paths), num_workers)
    _run(
        [{"paths": b, "names": names, "bounds": bounds, "load": load_columns} for b in batches],
        _coarse_job,
        num_workers,
        merge_coarse,
        "coarse",
    )
    targets: dict[str, np.ndarray] = {}
    residuals: dict[str, np.ndarray] = {}
    for name in names:
        targets[name], residuals[name] = coarse_targets(histograms[name], extrema[name][2])

    refined: dict[str, dict[str, np.ndarray]] = {}

    def merge_refined(partial: dict[str, dict[str, np.ndarray]]) -> None:
        for name, part in partial.items():
            if name not in refined:
                refined[name] = part
                continue
            refined[name]["count"] += part["count"]
            np.minimum(refined[name]["min"], part["min"], out=refined[name]["min"])
            np.maximum(refined[name]["max"], part["max"], out=refined[name]["max"])

    _run(
        [
            {"paths": b, "names": names, "bounds": bounds, "targets": targets, "load": load_columns}
            for b in batches
        ],
        _refine_job,
        num_workers,
        merge_refined,
        "refine",
    )
    quantiles: dict[str, dict[str, np.ndarray]] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for name in names:
        values, errors = finalize_quantiles(refined[name], residuals[name], extrema[name][2])
        quantiles[name] = values
        provenance[name] = {key: errors[key].tolist() for key in values}
    return quantiles, provenance


def _coarse_job(job: dict[str, Any]) -> dict[str, np.ndarray]:
    """Coarse counts summed over a batch of files (exact: counts add)."""
    out: dict[str, np.ndarray] = {}
    for path in job["paths"]:
        for name, values in job["load"](path, job["names"]).items():
            histogram = coarse_histogram(values, *job["bounds"][name])
            if name in out:
                out[name] += histogram
            else:
                out[name] = histogram
    return out


def _refine_job(job: dict[str, Any]) -> dict[str, dict[str, np.ndarray]]:
    """Refined counts / min / max over a batch of files (exact: add, min, max)."""
    out: dict[str, dict[str, np.ndarray]] = {}
    for path in job["paths"]:
        for name, values in job["load"](path, job["names"]).items():
            part = refine_histogram(values, *job["bounds"][name], job["targets"][name])
            if name not in out:
                out[name] = part
                continue
            out[name]["count"] += part["count"]
            np.minimum(out[name]["min"], part["min"], out=out[name]["min"])
            np.maximum(out[name]["max"], part["max"], out=out[name]["max"])
    return out


def provenance_payload(errors: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "stats_quantile_provenance",
        "method": METHOD,
        "population": "all_rows_of_published_data_parquet",
        "quantile_definition": "numpy.quantile linear interpolation between order statistics",
        "coarse_bins": COARSE_BINS,
        "refine_bins": REFINE_BINS,
        "max_abs_error_bound": errors,
        "note": (
            "Quantiles are recomputed from the full data; file-level quantiles are never "
            "averaged or min/max-merged."
        ),
    }
