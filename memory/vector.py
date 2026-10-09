import faiss
import numpy as np
import os
import pickle
import requests
import time
from pathlib import Path
from core.config import get_env_with_config

OLLAMA_HOST = get_env_with_config("ollama_host") or "http://localhost:11434"
OLLAMA_EMBED_URL = f"{OLLAMA_HOST}/api/embeddings"
MODEL = get_env_with_config("jade_model") or "llama3"

# Memory lives under the (possibly migrated) home config dir.
MEMORY_DIR = Path.home() / ".jade" / "memory"
DB_PATH = MEMORY_DIR / "memory_store.pkl"
INDEX_PATH = MEMORY_DIR / "memory.index"

_index = None
_store = []

def _get_embedding(text):
    try:
        r = requests.post(OLLAMA_EMBED_URL, json={
            "model": MODEL,
            "prompt": text
        })
        r.raise_for_status()
        return r.json()["embedding"]
    except Exception as e:
        # Silently fail for embeddings to not disrupt the UI flow
        return None

def _get_index(dim=None):
    """Load (or create) the FAISS index.

    dim: expected embedding dimension. When given and the on-disk index was
    built with a different dimension (e.g. the embedding model changed), the
    stale index is archived and a fresh one is created, since FAISS cannot
    mix dimensions in one index.
    """
    global _index, _store
    if _index is None:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if INDEX_PATH.exists():
            try:
                _index = faiss.read_index(str(INDEX_PATH))
            except Exception as e:
                print(f"[memory] stored index unreadable ({e}); starting fresh")
                _index = None
            if _index is not None and DB_PATH.exists():
                try:
                    with open(DB_PATH, "rb") as f:
                        _store = pickle.load(f)
                except Exception as e:
                    print(f"[memory] stored memories unreadable ({e}); starting fresh")
                    _store = []
        if _index is None:
            _index = faiss.IndexFlatL2(dim or 4096)
    if dim is not None and _index.d != dim:
        ts = int(time.time())
        try:
            INDEX_PATH.rename(INDEX_PATH.with_name(f"memory.index.bak-{ts}"))
            if DB_PATH.exists():
                DB_PATH.rename(DB_PATH.with_name(f"memory_store.pkl.bak-{ts}"))
        except OSError as e:
            print(f"[memory] could not archive stale index: {e}")
        print(f"[memory] embedding dimension changed ({_index.d} -> {dim}); "
              f"archived old index and started fresh")
        _index = faiss.IndexFlatL2(dim)
        _store = []
    return _index

def add(text, metadata=None):
    """Store a memory with optional metadata. Returns True on success."""
    if metadata:
        entry = f"[{metadata}] {text}"
    else:
        entry = text

    emb = _get_embedding(entry)
    if emb is None:
        return False

    try:
        index = _get_index(dim=len(emb))
        index.add(np.array([emb]).astype("float32"))
    except Exception as e:
        print(f"[memory] failed to store memory: {e}")
        return False
    _store.append(entry)

    try:
        faiss.write_index(index, str(INDEX_PATH))
        with open(DB_PATH, "wb") as f:
            pickle.dump(_store, f)
    except OSError as e:
        print(f"[memory] failed to persist memory: {e}")
        return False
    return True

def search(q, k=5):
    emb = _get_embedding(q)
    if emb is None: return []
    
    index = _get_index(dim=len(emb))
    if index.ntotal == 0:
        return []
        
    D, I = index.search(np.array([emb]).astype("float32"), k)
    # FAISS returns -1 for empty slots; filter those (and any out-of-range
    # ids) so _store[-1] can never leak the last entry by accident.
    return [_store[i] for i in I[0] if 0 <= i < len(_store)]

def clear():
    """Wipe all stored memories."""
    global _index, _store
    _index = None
    _store = []
    if INDEX_PATH.exists():
        os.remove(INDEX_PATH)
    if DB_PATH.exists():
        os.remove(DB_PATH)
    return "Memory successfully cleared."

def get_stats():
    """Return memory statistics."""
    index = _get_index()
    return {
        "count": len(_store),
        "index_size": index.ntotal if index else 0
    }
