"""Versioned, deterministic fingerprints for canonical bar series."""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import (
    infer_dtype,
    is_bool_dtype,
    is_datetime64_dtype,
    is_float_dtype,
    is_integer_dtype,
    is_timedelta64_dtype,
)

from ..domain import BarSeries


_ALGORITHM_MARKER = b"gold-research/bar-series-fingerprint/v2\0"
_U32 = struct.Struct(">I")
_U64 = struct.Struct(">Q")
_DIMENSIONS = struct.Struct(">QQ")


def _payload_view(payload: Any) -> memoryview:
    if isinstance(payload, np.ndarray):
        payload = np.ascontiguousarray(payload)
    view = memoryview(payload)
    if not view.c_contiguous:
        view = memoryview(np.ascontiguousarray(payload))
    return view.cast("B")


def _update_payload(digest: Any, label: bytes, payload: Any) -> None:
    view = _payload_view(payload)
    digest.update(_U32.pack(len(label)))
    digest.update(label)
    digest.update(_U64.pack(view.nbytes))
    digest.update(view)


def _update_text(digest: Any, label: bytes, value: str) -> None:
    _update_payload(digest, label, value.encode("utf-8"))


def _dtype_name(dtype: Any) -> str:
    dtype_type = type(dtype)
    return f"{dtype_type.__module__}.{dtype_type.__qualname__}"


def _dtype_descriptor(column: pd.Series) -> str:
    dtype = column.dtype
    if isinstance(dtype, pd.CategoricalDtype):
        return f"category:ordered={int(dtype.ordered)}"
    if isinstance(dtype, pd.DatetimeTZDtype):
        return f"datetime64:{dtype.unit}:aware-utc"
    if is_datetime64_dtype(dtype):
        return f"datetime64:{getattr(dtype, 'unit', 'ns')}:naive-utc"
    if is_timedelta64_dtype(dtype):
        return f"timedelta64:{getattr(dtype, 'unit', 'ns')}"
    if isinstance(dtype, np.dtype):
        if dtype.kind == "b":
            return "numpy:bool"
        if dtype.kind in "iu":
            return f"numpy:{'uint' if dtype.kind == 'u' else 'int'}:{dtype.itemsize * 8}"
        if dtype.kind == "f":
            return f"numpy:float:{dtype.itemsize * 8}"
        if dtype.kind in "SU":
            return f"numpy:{'bytes' if dtype.kind == 'S' else 'unicode'}:{dtype.itemsize}"
        if dtype.kind == "O":
            return f"numpy:object:{infer_dtype(column, skipna=True)}"
        return f"numpy:{dtype.kind}:{dtype.itemsize}"
    if is_bool_dtype(dtype):
        return f"{_dtype_name(dtype)}:bool:{dtype}"
    if is_integer_dtype(dtype):
        return f"{_dtype_name(dtype)}:int:{getattr(dtype, 'numpy_dtype', dtype)}"
    if is_float_dtype(dtype):
        return f"{_dtype_name(dtype)}:float:{getattr(dtype, 'numpy_dtype', dtype)}"
    if isinstance(dtype, pd.StringDtype):
        return f"{_dtype_name(dtype)}:string:{dtype}"
    return f"{_dtype_name(dtype)}:{dtype}:{infer_dtype(column, skipna=True)}"


def _null_mask(column: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    mask = column.isna().to_numpy(dtype=np.bool_, na_value=True)
    return mask, np.packbits(mask, bitorder="little")


def _numeric_target(dtype: Any) -> np.dtype:
    if is_bool_dtype(dtype):
        return np.dtype("u1")
    numpy_dtype = np.dtype(getattr(dtype, "numpy_dtype", dtype))
    if numpy_dtype.kind not in "iuf":
        raise TypeError(f"unsupported numeric dtype: {dtype}")
    return numpy_dtype.newbyteorder("<")


def _numeric_values(column: pd.Series, mask: np.ndarray) -> np.ndarray:
    target = _numeric_target(column.dtype)
    values = column.to_numpy(dtype=target, na_value=0, copy=False)
    values = np.ascontiguousarray(values, dtype=target)
    if mask.any():
        values = values.copy()
        values[mask] = 0
    return values


def _datetime_values(column: pd.Series, mask: np.ndarray) -> np.ndarray:
    parsed = pd.to_datetime(column, utc=True, errors="raise")
    values = parsed.array
    if hasattr(values, "as_unit"):
        values = values.as_unit("ns")
    else:
        values = values.astype("datetime64[ns]")
    result = np.ascontiguousarray(values.asi8, dtype="<i8")
    if mask.any():
        result = result.copy()
        result[mask] = 0
    return result


def _timedelta_values(column: pd.Series, mask: np.ndarray) -> np.ndarray:
    parsed = pd.to_timedelta(column, errors="raise")
    values = parsed.array
    if hasattr(values, "as_unit"):
        values = values.as_unit("ns")
    else:
        values = values.astype("timedelta64[ns]")
    result = np.ascontiguousarray(values.asi8, dtype="<i8")
    if mask.any():
        result = result.copy()
        result[mask] = 0
    return result


def _hash_text_values(digest: Any, column: pd.Series) -> None:
    encoded = column.astype("string").str.encode("utf-8")
    lengths = encoded.str.len().fillna(0).to_numpy(dtype=np.uint64, na_value=0)
    _update_payload(digest, b"text-lengths", lengths.astype(">u8", copy=False))
    joined = b"".join(encoded.fillna(b"").to_numpy(dtype=object).tolist())
    _update_payload(digest, b"text-bytes", joined)


def _hash_category_values(digest: Any, column: pd.Series, mask: np.ndarray) -> None:
    categories = pd.Series(column.cat.categories, copy=False)
    _update_text(digest, b"category-dtype", _dtype_descriptor(categories))
    _hash_text_values(digest, categories)
    codes = column.cat.codes.to_numpy(dtype="<i8", copy=False)
    codes = np.ascontiguousarray(codes, dtype="<i8")
    if mask.any():
        codes = codes.copy()
        codes[mask] = 0
    _update_payload(digest, b"category-codes", codes)


def _hash_column(digest: Any, column: pd.Series) -> None:
    _update_text(digest, b"dtype", _dtype_descriptor(column))
    mask, packed_mask = _null_mask(column)
    _update_payload(digest, b"null-count", _U64.pack(int(mask.sum())))
    _update_payload(digest, b"null-bits", packed_mask)
    dtype = column.dtype
    if isinstance(dtype, pd.CategoricalDtype):
        _hash_category_values(digest, column, mask)
    elif isinstance(dtype, pd.DatetimeTZDtype) or is_datetime64_dtype(dtype):
        _update_payload(digest, b"values", _datetime_values(column, mask))
    elif is_timedelta64_dtype(dtype):
        _update_payload(digest, b"values", _timedelta_values(column, mask))
    elif is_bool_dtype(dtype) or is_integer_dtype(dtype) or is_float_dtype(dtype):
        _update_payload(digest, b"values", _numeric_values(column, mask))
    else:
        _hash_text_values(digest, column)


def fingerprint_bar_series(series: BarSeries) -> str:
    """Return a deterministic SHA-256 fingerprint for one ``BarSeries``.

    The index is intentionally excluded, matching the previous ``to_csv``
    contract's ``index=False`` behavior. Column order remains part of the
    schema framing and therefore remains significant.
    """

    if not isinstance(series, BarSeries):
        raise TypeError("series must be a BarSeries")
    if not isinstance(series.bars, pd.DataFrame):
        raise TypeError("series.bars must be a pandas DataFrame")

    digest = hashlib.sha256()
    digest.update(_ALGORITHM_MARKER)
    metadata = json.dumps(
        series.metadata.to_dict(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    _update_payload(digest, b"metadata-json", metadata)
    _update_text(digest, b"timeframe", series.timeframe)

    frame = series.bars
    row_count, column_count = frame.shape
    _update_payload(digest, b"dimensions", _DIMENSIONS.pack(row_count, column_count))
    _update_text(digest, b"index-policy", "ignore-index-v1")
    for position in range(column_count):
        name = frame.columns[position]
        column = frame.iloc[:, position]
        _update_payload(digest, b"column-position", _U64.pack(position))
        _update_text(digest, b"column-name-type", _dtype_name(name))
        _update_text(digest, b"column-name", str(name))
        _hash_column(digest, column)
    return digest.hexdigest()
