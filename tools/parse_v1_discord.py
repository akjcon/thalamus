import json, re, sys
d = json.load(open(sys.argv[1]))
# manual symbol map: non-US listings, futures, renamed/defunct tickers
MAP = {"ZCN26":"ZC=F","ZCZ26":"ZC=F","INT":"WKC","FUEL":"WKC","RIGD":"RELIANCE.NS","BAS GY":"BAS.DE","BAS.DE":"BAS.DE",
       "GENL.L":"GENL.L","1COV.DE":"1COV.DE","LHA.DE":"LHA.DE","WIE.VI":"WIE.VI","UN01.DE":"UN01.DE","SGG":"SB=F","TEN":"TEN"}
KEY = [("TTF","TTF=F"),("Henry Hub","NG=F"),("Soybean Futures","ZS=F"),("Heating Oil","HO=F"),("Live Cattle","LE=F"),
       ("Sugar","SB=F"),("Corn Futures","ZC=F"),("corn futures","ZC=F")]
SKIP = {"TRTN":"taken private 2023","EURN":"ticker retired","EWK":"wrong instrument (EWK is a Belgium ETF)"}
def parse(line):
    m = re.match(r"\*\*\d+\.\s+(LONG|SHORT)\b(.*)", line.strip())
    if not m: return None
    side, rest = m.group(1), m.group(2)
    for k, sym in KEY:
        if k in rest.split("—")[0]: return side, sym, rest
    t = re.search(r"\(([A-Z0-9][A-Z0-9 ./]{0,11}?)(?:\s[—-]|\)|,)", rest)
    if not t: return side, None, rest
    raw = t.group(1).strip()
    return side, MAP.get(raw, raw), rest
ideas = []
for m in reversed(d["alerts"]):
    for e in m.get("embeds", []):
        if "ALERT" not in (e.get("title") or "").upper(): continue
        for l in (e.get("description") or "").split("\n"):
            p = parse(l)
            if p:
                verdict = "REVISE" if "REVISE" in l else "ALERT"
                ideas.append({"ts": m["timestamp"], "side": p[0], "sym": p[1], "kind": verdict, "text": p[2][:90].strip(" *")})
# red-team kills from logs
KILLS = {"Live Cattle":"LE=F","COPX":"COPX","GNK":"GNK","Sugar":"SB=F","IRDM":"IRDM"}
for m in reversed(d["thal-logs"]):
    for e in m.get("embeds", []):
        for f in e.get("fields", []):
            if "KILLED" in f["name"]:
                sym = next((v for k, v in KILLS.items() if k in f["name"]), None)
                ideas.append({"ts": m["timestamp"], "side": "LONG", "sym": sym, "kind": "KILL", "text": f["name"][18:100]})
for i in ideas:
    if i["sym"] in SKIP: i["skip"] = SKIP[i["sym"]]; 
json.dump(ideas, open(sys.argv[2], "w"), indent=1)
from collections import Counter
print(len(ideas), "ideas;", Counter(i["kind"] for i in ideas))
print(Counter(i["sym"] for i in ideas).most_common(80))
print("unparsed:", [i["text"] for i in ideas if not i["sym"]])
