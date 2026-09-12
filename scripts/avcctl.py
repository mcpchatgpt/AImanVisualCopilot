#!/usr/bin/env python3
import argparse, hashlib, json, secrets, sqlite3, subprocess, time
from pathlib import Path

ENV = Path('/etc/aiman-visual-copilot.env')
DB = Path('/var/lib/aiman-visual-copilot/avc.db')

def env_values():
    out={}
    for line in ENV.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1); out[k]=v
    return out

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def make_enrollment(label, ttl):
    token=secrets.token_urlsafe(32); now=time.time()
    c=db(); c.execute('insert into enrollments(token_hash,label,created_at,expires_at,used_at) values(?,?,?,?,null)',
                      (hashlib.sha256(token.encode()).hexdigest(),label,now,now+ttl*60)); c.commit(); c.close()
    return token

def main():
    ap=argparse.ArgumentParser(prog='avcctl')
    sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('status'); sub.add_parser('sources')
    p=sub.add_parser('windows-install-command'); p.add_argument('--label',default='MyWindows-v9'); p.add_argument('--ttl',type=int,default=15)
    p=sub.add_parser('remove-source'); p.add_argument('source'); p.add_argument('--yes',action='store_true')
    a=ap.parse_args(); e=env_values()
    if a.cmd=='status':
        subprocess.run(['systemctl','--no-pager','--full','status','aiman-visual-copilot.service'])
    elif a.cmd=='sources':
        c=db(); rows=c.execute('select id,label,device_id,hostname,platform,agent_version,last_seen from sources order by last_seen desc').fetchall(); c.close()
        print(json.dumps([dict(r) for r in rows],indent=2,ensure_ascii=False))
    elif a.cmd=='windows-install-command':
        ttl=max(1,min(a.ttl,120)); token=make_enrollment(a.label,ttl)
        url=e['AVC_PUBLIC_URL'].rstrip('/')+'/agent/windows-observer.ps1'; label=a.label.replace("'","''")
        print("$p=Join-Path $env:TEMP 'avc-observer.ps1'; "
              f"Invoke-WebRequest -UseBasicParsing '{url}' -OutFile $p; "
              f"& $p -Server '{e['AVC_PUBLIC_URL']}' -EnrollmentToken '{token}' -Label '{label}' -Install")
        print(f"# one-time enrollment; expires in {ttl} minutes")
    elif a.cmd=='remove-source':
        if not a.yes: raise SystemExit('add --yes')
        c=db(); r=c.execute('select id from sources where id=? or label=? or device_id=?',(a.source,a.source,a.source)).fetchone()
        if not r: raise SystemExit('source not found')
        sid=r['id']; paths=[x[0] for x in c.execute('select screenshot_path from frames where source_id=? and screenshot_path is not null',(sid,))]
        c.execute('delete from frames where source_id=?',(sid,)); c.execute('delete from sources where id=?',(sid,)); c.commit(); c.close()
        for p in paths:
            try: Path(p).unlink(missing_ok=True)
            except Exception: pass
        print(json.dumps({'ok':True,'removed':sid}))
if __name__=='__main__': main()
