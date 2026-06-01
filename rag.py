"""
RAG 모듈 — tetrapod0/RAG_with_lm_studio 방식 기반

사용법:
  1. rag_docs/ 폴더에 파일을 넣는다.
       rag_docs/code_examples/       ← 코드 예제 TXT/PDF
       rag_docs/problem_templates/   ← 문제 템플릿 TXT/PDF
  2. 서버를 시작하면 rag_docs/를 스캔해 자동 인덱싱.
  3. 파일을 추가·수정했을 때는 POST /api/rag/rebuild 호출.

LM Studio 사전 준비:
  - 채팅 모델과 별도로 임베딩 모델을 로드해야 함.
  - EMBED_MODEL 을 실제 모델명으로 변경.
"""

import os
import shutil
import logging
from typing import List, Optional

# ── 설정 ──────────────────────────────────────────────────────
LM_BASE_URL   = "http://localhost:1234/v1"
EMBED_MODEL   = "text-embedding-bge-m3"  # LM Studio에 로드된 임베딩 모델명
DOCS_DIR      = os.path.join(os.path.dirname(__file__), "rag_docs")   # 파일 보관 폴더
DB_DIR        = os.path.join(os.path.dirname(__file__), "rag_db")     # FAISS 인덱스 폴더
CHUNK_SIZE    = 200
CHUNK_OVERLAP = 50
TOP_K         = 5

COLLECTIONS   = ("problem_templates",)
SUPPORTED_EXT = (".txt", ".pdf")

# 폴더 초기화
for _col in COLLECTIONS:
    os.makedirs(os.path.join(DOCS_DIR, _col), exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)

# ── LangChain / FAISS 임포트 ───────────────────────────────────
_AVAILABLE = False
try:
    from langchain_core.embeddings import Embeddings
    from langchain_community.vectorstores import FAISS
    from langchain_community.vectorstores.utils import DistanceStrategy
    from langchain_community.document_loaders import TextLoader, PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from openai import OpenAI
    _AVAILABLE = True
except ImportError as e:
    logging.warning(f"[RAG] 비활성화 — 패키지 누락: {e}")


# ── LM Studio 임베딩 (reference: MyEmbeddings) ────────────────
if _AVAILABLE:
    class OllamaEmbeddings(Embeddings):
        def __init__(self):
            self._client = OpenAI(base_url=LM_BASE_URL, api_key="lm-studio")

        def embed_documents(self, texts: List[str]) -> List[List[float]]:
            texts = [t.replace("\n", " ") for t in texts]
            try:
                data = self._client.embeddings.create(input=texts, model=EMBED_MODEL).data
                return [d.embedding for d in data]
            except Exception as e:
                raise RuntimeError(
                    f"임베딩 실패: {e}\n"
                    f"LM Studio에 임베딩 모델({EMBED_MODEL})이 로드되어 있는지 확인하세요."
                ) from e

        def embed_query(self, text: str) -> List[float]:
            return self.embed_documents([text])[0]

    _embeddings = OllamaEmbeddings()

    _splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", "(?<=\\. )", " ", ""],
        length_function=len,
    )

    _stores: dict = {name: None for name in COLLECTIONS}
else:
    _embeddings = None
    _splitter   = None
    _stores     = {}


# ── 내부 유틸 ──────────────────────────────────────────────────
def _load_file(file_path: str) -> List:
    """TXT/PDF 파일을 LangChain Document 리스트로 로드 후 청킹."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".txt":
        loader = TextLoader(file_path, encoding="utf-8")
    elif ext == ".pdf":
        loader = PyPDFLoader(file_path)
    else:
        raise ValueError(f"지원하지 않는 형식: {ext}")
    return loader.load_and_split(text_splitter=_splitter)


def _save(collection: str):
    store = _stores.get(collection)
    if store:
        store.save_local(os.path.join(DB_DIR, collection))


def _check(collection: str):
    if collection not in COLLECTIONS:
        raise ValueError(f"알 수 없는 컬렉션: '{collection}'. 가능: {COLLECTIONS}")


def _build_index(collection: str) -> Optional[object]:
    """rag_docs/{collection}/ 의 모든 파일을 읽어 FAISS 인덱스를 새로 빌드."""
    docs_path = os.path.join(DOCS_DIR, collection)
    all_chunks = []

    for fname in sorted(os.listdir(docs_path)):
        if not any(fname.lower().endswith(ext) for ext in SUPPORTED_EXT):
            continue
        fpath = os.path.join(docs_path, fname)
        try:
            chunks = _load_file(fpath)
            for c in chunks:
                c.metadata["source_file"] = fname
            all_chunks.extend(chunks)
            logging.info(f"[RAG] '{collection}' ← {fname} ({len(chunks)}청크)")
        except Exception as e:
            logging.warning(f"[RAG] {fname} 로드 실패: {e}")

    if not all_chunks:
        return None

    return FAISS.from_documents(
        all_chunks, _embeddings, distance_strategy=DistanceStrategy.COSINE
    )


# ── 서버 시작 시 자동 인덱싱 ──────────────────────────────────
def _init():
    """
    컬렉션별로:
      - FAISS 인덱스가 이미 있으면 → 그대로 로드 (빠름)
      - 없으면 → rag_docs/ 스캔해서 새로 빌드
    """
    if not _AVAILABLE:
        return
    for col in COLLECTIONS:
        idx_path = os.path.join(DB_DIR, col)
        if os.path.exists(idx_path):
            try:
                _stores[col] = FAISS.load_local(
                    idx_path, _embeddings, allow_dangerous_deserialization=True
                )
                logging.info(f"[RAG] '{col}' 인덱스 로드 완료 ({_stores[col].index.ntotal}벡터)")
                continue
            except Exception as e:
                logging.warning(f"[RAG] '{col}' 인덱스 로드 실패, 재빌드: {e}")

        # 인덱스 없음 → rag_docs/ 에 파일이 있으면 빌드
        try:
            store = _build_index(col)
            if store:
                _stores[col] = store
                _save(col)
                logging.info(f"[RAG] '{col}' 인덱스 빌드 완료")
            else:
                logging.info(f"[RAG] '{col}' — rag_docs/{col}/ 에 파일 없음, 비어 있음")
        except Exception as e:
            logging.warning(f"[RAG] '{col}' 빌드 실패 (LM Studio 미실행?): {e}")

_init()


# ── 공개 API ───────────────────────────────────────────────────

def retrieve(collection: str, query: str, k: int = TOP_K) -> str:
    """query와 유사한 청크 k개를 반환. 인덱스 없으면 ''."""
    if not _AVAILABLE:
        return ""
    _check(collection)
    store = _stores.get(collection)
    if store is None:
        return ""
    try:
        docs = store.similarity_search(query, k=k)
        return "\n---\n".join(d.page_content for d in docs)
    except Exception as e:
        logging.warning(f"[RAG] retrieve 오류: {e}")
        return ""


def rebuild(collection: str = None):
    """
    rag_docs/ 를 다시 스캔해서 FAISS 인덱스를 재빌드.
    collection=None 이면 전체 컬렉션 재빌드.
    파일을 추가·수정한 뒤 호출하면 됨.
    """
    if not _AVAILABLE:
        return {}
    targets = COLLECTIONS if collection is None else (collection,)
    result = {}
    for col in targets:
        _check(col)
        # 기존 인덱스 삭제
        idx_path = os.path.join(DB_DIR, col)
        if os.path.exists(idx_path):
            shutil.rmtree(idx_path)
        _stores[col] = None

        store = _build_index(col)
        if store:
            _stores[col] = store
            _save(col)
        result[col] = _stores[col].index.ntotal if _stores[col] else 0
        logging.info(f"[RAG] '{col}' 재빌드 완료: {result[col]}벡터")
    return result


def counts() -> dict:
    """각 컬렉션의 벡터(청크) 수."""
    result = {}
    for name in COLLECTIONS:
        store = _stores.get(name)
        try:
            result[name] = store.index.ntotal if store else 0
        except Exception:
            result[name] = 0
    return result


def list_docs(collection: str = None) -> dict:
    """rag_docs/ 에 있는 파일 목록 반환."""
    targets = COLLECTIONS if collection is None else (collection,)
    result = {}
    for col in targets:
        docs_path = os.path.join(DOCS_DIR, col)
        result[col] = [
            f for f in sorted(os.listdir(docs_path))
            if any(f.lower().endswith(ext) for ext in SUPPORTED_EXT)
        ]
    return result
