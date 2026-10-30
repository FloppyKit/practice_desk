"""Undo apply-bridge-host-20261030: restore the Lambda env (old URL + old token) from the 600 backup.
If Caddy no longer accepts the old token, also restore caddy/Caddyfile.bak-20261030-pretoken on do-edge and reload."""
import boto3, json, os
FN='practice-desk-bridge'
BK=os.path.expanduser('~/.config/practice-desk/tmp/lambda-env.pre-bridgehost-20261030.json')
l=boto3.client('lambda',region_name='us-east-1')
l.update_function_configuration(FunctionName=FN,Environment={'Variables':json.load(open(BK))})
l.get_waiter('function_updated_v2').wait(FunctionName=FN)
print('OK lambda env restored (hooks.psycharts.org + old token)')
