import hashlib

from inspeg.store.blobstore import BlobStore


def test_put_get_roundtrip(tmp_path):
    blobs = BlobStore(tmp_path)
    data = b"hello inspeg"
    digest, rel = blobs.put(data)
    assert digest == hashlib.sha256(data).hexdigest()
    assert rel == f"blobs/{digest[:2]}/{digest}"
    assert (tmp_path / rel).is_file()
    assert blobs.get(digest) == data


def test_dedup_is_automatic(tmp_path):
    blobs = BlobStore(tmp_path)
    d1, _ = blobs.put(b"same bytes")
    d2, _ = blobs.put(b"same bytes")
    assert d1 == d2
    shard = tmp_path / "blobs" / d1[:2]
    assert len(list(shard.iterdir())) == 1


def test_distinct_content_distinct_paths(tmp_path):
    blobs = BlobStore(tmp_path)
    d1, _ = blobs.put(b"one")
    d2, _ = blobs.put(b"two")
    assert d1 != d2
    assert blobs.exists(d1) and blobs.exists(d2)
