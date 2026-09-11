import datetime,json,pathlib,subprocess,time
p=pathlib.Path(__file__).resolve().parent
first=(p/'orchestrate_001.json').stat().st_mtime
deadline=first+6*3600-120
last=first
args={'seed':140701,'partition':'priority'}
for n in range(2,100):
    due=last+300
    while time.time()<due:
        time.sleep(min(30,due-time.time()))
    if time.time()>=deadline:
        print('TIME_LIMIT',flush=True);break
    start=datetime.datetime.now(datetime.timezone.utc).isoformat()
    r=subprocess.run(['curl','-sS','--max-time','240','-X','POST','http://127.0.0.1:8000/orchestrate_jam25','-H','Content-Type: application/json','-d',json.dumps(args)],capture_output=True,text=True)
    last=time.time()
    name=f'orchestrate_{n:03d}.json'
    (p/name).write_text(r.stdout)
    try: data=json.loads(r.stdout)
    except Exception: data={'parse_error':True,'raw':r.stdout}
    e={'start':start,'end':datetime.datetime.now(datetime.timezone.utc).isoformat(),'tool':'orchestrate_jam25','args':args,'returncode':r.returncode,'stderr':r.stderr,'response_file':'logs/'+name,'response':data}
    with (p/'timeline.jsonl').open('a') as f:f.write(json.dumps(e,ensure_ascii=False)+'\n')
    print(json.dumps(e,ensure_ascii=False),flush=True)
    if data.get('stage')=='complete' or 'diverged' in json.dumps(data).lower():break
