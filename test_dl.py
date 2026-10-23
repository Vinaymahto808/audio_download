import requests, time
urls=[
 "https://recordings.mcube.com/mcubefiles110/appmcube/2026/10/7887/84680049951792563593.wav",
 "https://recordings.mcube.com/mcubefiles110/appmcube/2026/10/7887/99908482781792561701.wav",
]
h={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
for u in urls:
    try:
        t=time.time()
        r=requests.get(u,headers=h,timeout=30,stream=True)
        print(u[-40:], r.status_code, r.headers.get("Content-Type"), r.headers.get("Content-Length"), f"{time.time()-t:.2f}s")
        r.close()
    except Exception as e:
        print(u[-40:], "ERR", type(e).__name__, e)
# try a bogus id to see 404 behaviour
r=requests.get("https://recordings.mcube.com/mcubefiles110/appmcube/2026/10/7887/0000000000000000000.wav",headers=h,timeout=30)
print("bogus:", r.status_code, r.headers.get("Content-Type"), len(r.content))
