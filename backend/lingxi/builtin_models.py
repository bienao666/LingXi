"""Immutable upstream revisions; changing any field changes the index identity."""
BUILTIN_MODELS = {
    'embedding': {'name': 'BAAI/bge-m3', 'revision': '5617a9f61b028005a4858fdac845db406aefb181',
        'implementation': 'cls-normalized-1022-token-chunks-mean-v1'},
    'rerank': {'name': 'BAAI/bge-reranker-v2-m3', 'revision': '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e',
        'implementation': '512-token-document-chunks-max-sigmoid-v1'},
}
