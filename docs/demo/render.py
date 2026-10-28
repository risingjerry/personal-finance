"""Rebuild docs/demo.gif from the real `python3 -m pfin demo` output (made-up data only).

    python3 docs/demo/render.py          # needs Google Chrome and ffmpeg

Frames are terminal-styled HTML pages screenshotted by headless Chrome at 2x, then joined with
ffmpeg palettegen/paletteuse. No terminal recorder is involved.
"""
import html,os,shutil,subprocess,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
CHROME=os.environ.get('CHROME','/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
W,H=960,330
CMD='python3 -m pfin demo'
CSS="""body{margin:0;background:#0d1117;font:15px/1.55 Menlo,'SF Mono',Monaco,'PingFang SC',monospace;color:#c9d1d9}
.win{margin:18px;border-radius:10px;background:#161b22;box-shadow:0 8px 30px #0008;overflow:hidden;height:%dpx}
.bar{height:30px;background:#21262d;display:flex;align-items:center;padding-left:14px;gap:8px}
.dot{width:12px;height:12px;border-radius:50%%}.t{color:#8b949e;font-size:12px;margin-left:auto;margin-right:auto;transform:translateX(-30px)}
pre{margin:0;padding:14px 18px;white-space:pre}.p{color:#7ee787}.k{color:#d2a8ff}.n{color:#ffa657}.s{color:#79c0ff}.d{color:#8b949e}.y{color:#f2cc60}
.c{background:#c9d1d9;color:#0d1117}""" % (H-36)


def color(line):
 e=html.escape(line)
 if ' <- ' in line:
  head,_,src=line.partition('<- ')  # keep the demo's own column padding
  return f'<span class="d">{head[:11]}</span>  <span class="k">{head[13:22]}</span><span class="n">{head[22:31]}</span>  <span style="display:inline-block;width:15ch">{html.escape(head[31:].strip())}</span><span class="d">&lt;-</span> <span class="s">{html.escape(src)}</span>'
 if line.startswith(('personal:','business:','investing:')):return f'<span class="y">{e}</span>'
 if line.startswith('needs your decision'):return f'<span class="p">{e}</span>'
 if '->' in line:return f'<b>{e}</b>'
 return e


def page(lines,typed,cursor):
 body=f'<span class="p">$</span> {html.escape(typed)}'+('<span class="c"> </span>' if cursor else '')
 if lines:body+='\n'+'\n'.join(color(l) for l in lines)
 return f'<html><head><meta charset="utf-8"><style>{CSS}</style></head><body><div class="win"><div class="bar">'\
  '<span class="dot" style="background:#ff5f56"></span><span class="dot" style="background:#ffbd2e"></span>'\
  f'<span class="dot" style="background:#27c93f"></span><span class="t">pfin — made-up data</span></div><pre>{body}</pre></div></body></html>'


def main():
 out=subprocess.run([sys.executable,'-m','pfin','demo'],cwd=ROOT,capture_output=True,text=True,check=True).stdout
 lines=[l for l in out.splitlines() if not l.startswith('throwaway data folder')]
 while lines and not lines[-1].strip():lines.pop()
 frames=[]  # (html, seconds)
 for i in range(len(CMD)+1):frames.append((page([],CMD[:i],True),0.06 if i else 0.6))
 frames.append((page([],CMD,False),0.4))
 for i in range(1,len(lines)+1):frames.append((page(lines[:i],CMD,False),0.35 if lines[i-1].strip() else 0.1))
 frames.append((page(lines,CMD,False),4.0))
 tmp=Path(tempfile.mkdtemp(prefix='pfin-gif-'))
 concat=[]
 for n,(doc,secs) in enumerate(frames):
  f=tmp/f'{n:03d}.html';f.write_text(doc,encoding='utf-8')
  png=tmp/f'{n:03d}.png'
  subprocess.run([CHROME,'--headless=new','--disable-gpu','--hide-scrollbars','--force-device-scale-factor=2',f'--window-size={W},{H}',
   f'--screenshot={png}',f.as_uri()],check=True,capture_output=True)
  concat+= [f"file '{png}'",f'duration {secs}']
 concat.append(f"file '{tmp/f'{len(frames)-1:03d}.png'}'")
 (tmp/'list.txt').write_text('\n'.join(concat))
 gif=ROOT/'docs'/'demo.gif'
 subprocess.run(['ffmpeg','-y','-loglevel','error','-f','concat','-safe','0','-i',str(tmp/'list.txt'),'-vf',
  f'scale={W}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=none','-loop','0',str(gif)],check=True)
 shutil.rmtree(tmp)
 print(f'{gif} {gif.stat().st_size//1024} KB, {len(frames)} frames')


if __name__=='__main__':main()
