"""Four fixed compiler-only reductions. These have no original target and cannot count as matches.
Usage: python docs/plans/slus-reload-repros.py OUTPUT_DIR
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from decompnet.project import compiler_config

out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=False)
cc=compiler_config(out,{'compiler':{'toolchain':'gcc-2.7.2-psx','flags':['-O2','-G8']}})
locals_='\n'.join(f'  int x{i} = in[{i}];' for i in range(10))
updates='\n'.join(f'    x{i} += step({i});' for i in range(10))
uses='+'.join(f'x{i}' for i in range(10))
stack='extern int step(int);\nint probe(int *out,int *in,int n) {\n'+locals_+'\n RESERVE\n while(n--) {\n'+updates+'\n }\n *out='+uses+'; return x0;\n}\n'
grid='''extern void draw(int,int,int,int,unsigned int,int);
void probe(const unsigned char *s,int bx,int by) {
 int i; unsigned int ch;
 for(i=0;i<102;i++) {
  ch=*s++;
  if(ch && ch<96) draw(bx+REMAINDER*14+2,by+(i/17)*14-1,15,15,ch,0);
 }
}
'''
sources={'stack_plain':stack.replace('RESERVE',''),
         'stack_reserve':stack.replace('RESERVE','volatile int reserved[2];'),
         'grid_plain':grid.replace('REMAINDER','(i%17)'),
         'grid_quotient':grid.replace('REMAINDER','(i-(i/17)*17)')}
(out/'manifest.json').write_text(json.dumps({'compiles':4,'compiler':cc.cmd,'sources':sources},indent=2))
results=[]
for name,source in sources.items():
 d=out/name;d.mkdir();(d/'input.c').write_text(source);(d/'context.h').write_text('')
 argv=[a.replace('{src}',str(d/'input.c')).replace('{obj}',str(d/'output.o')).replace('{include}',str(d)) for a in cc.cmd]+['-da']
 r=subprocess.run(argv,cwd=d,capture_output=True,text=True,errors='replace',timeout=40)
 (d/'command.json').write_text(json.dumps(argv,indent=2));(d/'compiler.log').write_text(r.stdout+r.stderr)
 assembly=(d/'output.o.s').read_text() if (d/'output.o.s').exists() else ''
 results.append({'name':name,'returncode':r.returncode,'frame':re.findall(r'\.frame[^\n]+',assembly),
                 'hi_sequence':[line.strip() for line in assembly.splitlines() if 'mfhi' in line or re.search(r'\bsra\b',line)]})
(out/'results.json').write_text(json.dumps(results,indent=2));print(json.dumps(results))
