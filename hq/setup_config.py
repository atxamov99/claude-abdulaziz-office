"""Create local configuration without overwriting existing files."""
import json
import os
import sys
from pathlib import Path

def create(root, python):
    files={
        '.env':(root/'.env.example').read_text(),
        'projects.json':json.dumps({'projects':{'hq':str(root)},'topics':{}},indent=2),
        'mcp.json':json.dumps({'mcpServers':{'hq':{'command':python,'args':[str(root/'mcp_tg.py')],'env':{'HQ_HTTP_PORT':'8765'}}}},indent=2),
    }
    for name,text in files.items():
        try:
            fd=os.open(root/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            print(f'Kept existing {name}')
            continue
        with os.fdopen(fd,'w') as f:f.write(text+'\n')
        print(f'Created {name}')

if __name__=='__main__':
    create(Path(__file__).resolve().parent,sys.executable)
