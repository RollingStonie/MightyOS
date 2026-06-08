import os, json, asyncio, aiohttp
HERE = os.path.dirname(os.path.abspath(__file__))
envf = os.path.join(HERE, "bot.env")
if os.path.exists(envf):
    for line in open(envf, encoding="utf-8"):
        line=line.strip()
        if line and not line.startswith("#") and "=" in line:
            k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip())
AGENT=os.environ.get("AGENT","Agent")
BOT_TOKEN=os.environ["DISCORD_BOT_TOKEN"]
DKEY=os.environ.get("DEEPSEEK_API_KEY","sk-123dda9dcd104362929f51dc48cb88c1")
DURL=os.environ.get("DEEPSEEK_BASE_URL","https://api.deepseek.com")
SYS=os.environ.get("AGENT_SYS",f"You are {AGENT}, a fleet agent. Be concise and direct.")
API="https://discord.com/api/v10"; GW="wss://gateway.discord.gg/?v=10&encoding=json"
H={"Authorization":f"Bot {BOT_TOKEN}"}
async def me(s):
    async with s.get(f"{API}/users/@me",headers=H) as r: return (await r.json())["id"]
async def ask(s,msg):
    try:
        async with s.post(f"{DURL}/chat/completions",headers={"Authorization":f"Bearer {DKEY}","Content-Type":"application/json"},
            json={"model":"deepseek-chat","messages":[{"role":"system","content":SYS},{"role":"user","content":msg}]}) as r:
            return (await r.json())["choices"][0]["message"]["content"]
    except Exception as e: return f"(Mike error: {e})"
async def send(s,ch,c):
    await s.post(f"{API}/channels/{ch}/messages",headers={**H,"Content-Type":"application/json"},json={"content":c[:1900]})
async def run():
    async with aiohttp.ClientSession() as s:
        mid=await me(s); print(f"[{AGENT}] Bot ID {mid}",flush=True)
        while True:
            try:
                async with s.ws_connect(GW) as ws:
                    hb=None; seq=None
                    async def beat():
                        while True:
                            await asyncio.sleep(hb/1000); await ws.send_json({"op":1,"d":seq})
                    async for m in ws:
                        if m.type==aiohttp.WSMsgType.TEXT:
                            d=json.loads(m.data); op=d.get("op"); t=d.get("t")
                            if d.get("s"): seq=d["s"]
                            if t in ("READY","GUILD_CREATE","MESSAGE_CREATE"): print(f"[{AGENT}] evt {t}",flush=True)
                            if op==10:
                                hb=d["d"]["heartbeat_interval"]; asyncio.create_task(beat())
                                await ws.send_json({"op":2,"d":{"token":BOT_TOKEN,"intents":513,"properties":{"os":"windows","browser":AGENT,"device":AGENT}}})
                                print(f"[{AGENT}] connected",flush=True)
                            elif t=="MESSAGE_CREATE":
                                if d["d"].get("author",{}).get("id")==mid: continue
                                if mid in [x["id"] for x in d["d"].get("mentions",[])]:
                                    c=d["d"].get("content","").replace(f"<@{mid}>","").strip() or "Hi"
                                    print(f"[{AGENT}] msg: {c[:50]}",flush=True)
                                    await send(s,d["d"]["channel_id"], await ask(s,c))
                        elif m.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.ERROR):
                            print(f"[{AGENT}] closed {ws.close_code}",flush=True); break
            except Exception as e:
                print(f"[{AGENT}] error: {e}",flush=True)
            print(f"[{AGENT}] reconnecting in 5s...",flush=True)
            await asyncio.sleep(5)
asyncio.run(run())
