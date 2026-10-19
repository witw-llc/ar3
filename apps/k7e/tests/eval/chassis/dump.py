import re, sys
from sim import SPIKE, WT
sys.path.insert(0, str(WT / 'apps/k7e')); sys.path.insert(0, str(WT / 'lib'))
import engine
run=sys.argv[1]; pat=sys.argv[2] if len(sys.argv)>2 else None
engine.reset(SPIKE / 'runs' / run / 'home')
for p in sorted(engine._all_node_files()):
    t=p.read_text(); m=engine._parse_frontmatter(t)
    if pat and not re.search(pat,t): continue
    print('==',p.stem, m.get('status'), m.get('title'), m.get('tags'), 'vf',str(m.get('valid_from'))[:10],'vt',m.get('valid_to'),'sb',m.get('superseded_by'),'sa',str(m.get('superseded_at'))[:10])
    if len(sys.argv)>3: print(engine._extract_body(t))
