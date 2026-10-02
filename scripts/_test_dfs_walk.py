import ast
from pathlib import Path
import uuid

src = open('/home/suryahalim/doc-pipeline/extractor/app.py').read()
tree = ast.parse(src)
fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_walk_find_docs'][0]
ns = {'Path': Path, 'os': __import__('os'), 'MIME_SUFFIX': {'application/pdf': 'pdf', 'image/jpeg': 'jpg',
                                     'image/png': 'png', 'image/webp': 'webp',
                                     'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
                                     'text/plain': 'txt'}}
exec(compile(ast.Module(body=[fn], type_ignores=[]), '<w>', 'exec'), ns)

root = Path('/tmp/fsstest_' + uuid.uuid4().hex[:6])
(root / 'a/sub2/deep').mkdir(parents=True)
(root / '.hidden').mkdir()
for p in ['root1.pdf', 'a/nested.pdf', 'a/sub2/deep/deepest.pdf', 'a/notes.txt', '.hidden/x.pdf', 'b.jpg']:
    f = root / p
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b'x')

got = sorted(str(f.relative_to(root)) for f in ns['_walk_find_docs'](root))
print('found:', got)
assert got == ['a/nested.pdf', 'a/notes.txt', 'a/sub2/deep/deepest.pdf', 'b.jpg', 'root1.pdf'], got
print('DFS walker OK: nested pdfs found at every depth, hidden skipped')
