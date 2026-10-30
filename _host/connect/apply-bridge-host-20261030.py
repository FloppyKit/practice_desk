"""BRIDGE_BASE_URL hooks.psycharts.org -> hooks.drmattbrown.com and rotate BRIDGE_TOKEN.
Flow/modules invoke practice-desk-bridge unqualified ($LATEST), so the env change is live at once; a version is published for the record.
New token is read from ~/.config/practice-desk/tmp/bridge.new (never printed). Env backup (contains the old token) goes to
~/.config/practice-desk/tmp/lambda-env.pre-bridgehost-20261030.json (600), outside the repo."""
import boto3, json, os
FN='practice-desk-bridge'; R='us-east-1'
T=os.path.expanduser('~/.config/practice-desk/tmp'); BK=os.path.join(T,'lambda-env.pre-bridgehost-20261030.json')
l=boto3.client('lambda',region_name=R)
env=l.get_function_configuration(FunctionName=FN)['Environment']['Variables']
assert env['BRIDGE_BASE_URL'].startswith('https://hooks.psycharts.org'), 'unexpected BRIDGE_BASE_URL; stop'
if not os.path.exists(BK):
    fd=os.open(BK,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,json.dumps(env).encode()); os.close(fd)
new=dict(env); new['BRIDGE_BASE_URL']=env['BRIDGE_BASE_URL'].replace('https://hooks.psycharts.org','https://hooks.drmattbrown.com')
new['BRIDGE_TOKEN']=open(os.path.join(T,'bridge.new')).read().strip(); assert len(new['BRIDGE_TOKEN'])==64
l.update_function_configuration(FunctionName=FN,Environment={'Variables':new})
l.get_waiter('function_updated_v2').wait(FunctionName=FN)
v=l.publish_version(FunctionName=FN,Description='bridge host hooks.drmattbrown.com + token rotation 20261030')['Version']
print(f'OK lambda env updated ($LATEST live), published v{v}')
