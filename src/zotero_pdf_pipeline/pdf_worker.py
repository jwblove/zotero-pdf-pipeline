"""Bounded PDF inspection worker. No network or executable PDF content is used."""
from __future__ import annotations
import contextlib
import json
import sys

MAX_PAGES = 20000
MAX_INSPECT_PAGES = 2
MAX_TEXT = 32768

def inspect_pdf(path):
    try:
        from pypdf import PdfReader
        with contextlib.redirect_stdout(sys.stderr):
            reader = PdfReader(path, strict=False)
            root = getattr(reader, 'root_object', None)
            if root is None: root = reader.trailer['/Root']  # pypdf 4 compatibility
            declared = int(root['/Pages'].get('/Count', 0))
            if declared > MAX_PAGES: return {'error':'page limit'}
            pages = len(reader.pages)
            if not 0 < pages <= MAX_PAGES: return {'error':'page count'}
            chunks = []
            successes = 0
            for index in range(min(MAX_INSPECT_PAGES, pages)):
                try:
                    chunks.append((reader.pages[index].extract_text() or '')[:MAX_TEXT])
                    successes += 1
                except Exception:
                    chunks.append('')
            if not successes: return {'error':'text extraction failed'}
            # Preserve page boundaries, including a failed first-page extraction.
            return {'pages':pages, 'text':'\f'.join(chunks)[:MAX_TEXT]}
    except Exception:
        return {'error':'PDF parsing failed'}

def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    except (ImportError, ValueError, OSError):
        pass
    print(json.dumps(inspect_pdf(sys.argv[1]), ensure_ascii=True))

if __name__ == '__main__': main()
