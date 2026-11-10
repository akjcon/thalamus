import sys, json, time, urllib.request
env = dict(l.split("=",1) for l in open(sys.argv[1]).read().splitlines() if "=" in l and not l.startswith("#"))
H = {"Authorization": "Bot " + env["DISCORD_BOT_TOKEN"].strip(), "User-Agent": "DiscordBot (local, 1.0)"}
def get(p):
    return json.load(urllib.request.urlopen(urllib.request.Request("https://discord.com/api/v10" + p, headers=H)))
out = {}
for g in get("/users/@me/guilds"):
    for c in get(f"/guilds/{g['id']}/channels"):
        if c["name"] in ("thal-logs", "alerts"):
            allm, before = [], None
            while True:
                msgs = get(f"/channels/{c['id']}/messages?limit=100" + (f"&before={before}" if before else ""))
                if not msgs: break
                allm += msgs; before = msgs[-1]["id"]; time.sleep(0.4)
            out[c["name"]] = allm
            print(c["name"], len(allm), allm[-1]["timestamp"][:10], "→", allm[0]["timestamp"][:10])
json.dump(out, open(sys.argv[2], "w"))
