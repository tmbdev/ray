from typing import Any, Callable, Dict, TYPE_CHECKING
import tarfile
import io
import time
import re
import uuid

from ray.util.annotations import PublicAPI
from ray.data.block import BlockAccessor
from ray.data.datasource.file_based_datasource import FileBasedDatasource


if TYPE_CHECKING:
    import pyarrow

verbose_open = False


def base_plus_ext(path):
    """Split off all file extensions.

    Returns base, allext.

    :param path: path with extensions
    :param returns: path with all extensions removed

    """
    match = re.match(r"^((?:.*/|)[^.]+)[.]([^/]*)$", path)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def valid_sample(sample):
    """Check whether a sample is valid.

    :param sample: sample to be checked
    """
    return (
        sample is not None
        and isinstance(sample, dict)
        and len(list(sample.keys())) > 0
        and not sample.get("__bad__", False)
    )


def tar_file_iterator(fileobj, skipfn=lambda fname: False, meta={}):
    """Iterate over tar file, yielding filename, content pairs for the given tar stream.

    :param fileobj: byte stream suitable for tarfile
    :param skip_meta: regexp for keys that are skipped entirely (Default value = r"__[^/]*__($|/)")

    """
    global verbose_open
    stream = tarfile.open(fileobj=fileobj, mode="r|*")
    if verbose_open:
        print(f"start {meta}")
    for tarinfo in stream:
        fname = tarinfo.name
        if not tarinfo.isreg() or fname is None or skipfn(fname):
            continue
        data = stream.extractfile(tarinfo).read()
        result = dict(fname=fname, data=data, **meta)
        yield result
    if verbose_open:
        print(f"done {meta}")


def group_by_keys(data, keys=base_plus_ext, lcase=True, suffixes=None, trace=False):
    """Return function over iterator that groups key, value pairs into samples.

    :param keys: function that splits the key into key and extension (base_plus_ext)
    :param lcase: convert suffixes to lower case (Default value = True)
    """
    current_sample = None
    for filesample in data:
        assert isinstance(filesample, dict)
        fname, value = filesample["fname"], filesample["data"]
        prefix, suffix = keys(fname)
        if trace:
            print(
                prefix,
                suffix,
                current_sample.keys() if isinstance(current_sample, dict) else None,
            )
        if prefix is None:
            continue
        if lcase:
            suffix = suffix.lower()
        if current_sample is None or prefix != current_sample["__key__"]:
            if valid_sample(current_sample):
                yield current_sample
            current_sample = dict(__key__=prefix)
            if "__url__" in filesample:
                current_sample["__url__"] = filesample["__url__"]
        if suffix in current_sample:
            raise ValueError(
                f"{fname}: duplicate file name in tar file {suffix} {current_sample.keys()}"
            )
        if suffixes is None or suffix in suffixes:
            current_sample[suffix] = value
    if valid_sample(current_sample):
        yield current_sample

def table_to_list(table):
    """Convert a pyarrow table to a list of dictionaries.

    :param table: pyarrow table
    """
    result = []
    for i in range(table.num_rows):
        row = {}
        for name in table.column_names:
            row[name] = table[name][i].as_py()
        result.append(row)
    return result

@PublicAPI(stability="alpha")
class WebDatasetDatasource(FileBasedDatasource):
    _FILE_EXTENSION = "tar"

    def _read_stream(self, stream: "pyarrow.NativeFile", path: str, **kw):
        import pyarrow as pa

        files = tar_file_iterator(stream, meta=dict(__url__=path))
        samples = group_by_keys(files)
        for sample in samples:
            sample = {k: [v] for k, v in sample.items()}
            yield pa.Table.from_pydict(sample)

    def _write_block(
        self,
        f: "pyarrow.NativeFile",
        block: BlockAccessor,
        writer_args_fn: Callable[[], Dict[str, Any]] = lambda: {},
        **kw,
    ):
        import pyarrow
        table = block.to_arrow()
        # to_pylist is missing from these tables for some reason
        # samples = table.to_pylist()
        samples = table_to_list(table)
        stream = tarfile.open(fileobj=f, mode="w|")
        for sample in samples:
            if not "__key__" in sample:
                sample["__key__"] = uuid.uuid4().hex
            key = sample["__key__"]
            for k, v in sample.items():
                if v is None or k.startswith("__"):
                    continue
                assert isinstance(v, bytes) or isinstance(v, str)
                if not isinstance(v, bytes):
                    v = v.encode("utf-8")
                ti = tarfile.TarInfo(f"{key}.{k}")
                ti.size = len(v)
                ti.mtime = time.time()
                ti.mode, ti.uname, ti.gname = 0o644, "data", "data"
                stream.addfile(ti, io.BytesIO(v))
        stream.close()
