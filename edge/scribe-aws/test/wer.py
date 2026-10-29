import re, sys
def norm(t):
    t=t.lower().replace("n-acetylcysteine","n acetylcysteine")
    t=re.sub(r"spk_\d+:","",t)
    t=re.sub(r"[^a-z0-9' ]+"," ",t)
    return t.split()
ref=norm(" ".join(l.split("|",1)[1] for l in open("script.txt") if "|" in l))
hyp=norm(open(sys.argv[1]).read())
d=list(range(len(hyp)+1))
for i,r in enumerate(ref,1):
    prev,d[0]=d[0],i
    for j,h in enumerate(hyp,1):
        cur=min(d[j]+1,d[j-1]+1,prev+(r!=h)); prev,d[j]=d[j],cur
print(f"ref_words={len(ref)} hyp_words={len(hyp)} WER={d[-1]/len(ref):.1%}")
