"""Undo apply-voice-smsconsent-20261028: flow -> agent :17 (uses verify module v3), lambda code -> pre-smsconsent backup."""
import boto3, os, re
R='us-east-1'; I='e19b711e-6756-4e8b-a486-7407df3c9db5'; F='37247481-2117-4b24-aac2-53bfe71c8634'; AG='b210d774-9a6b-4536-88b0-c8b30e4ed386'
BK=os.path.join(os.path.dirname(os.path.abspath(__file__)),'..','voice-backups')
c=boto3.client('connect',region_name=R); l=boto3.client('lambda',region_name=R)
s=c.describe_contact_flow(InstanceId=I,ContactFlowId=F)['ContactFlow']['Content']
c.update_contact_flow_content(InstanceId=I,ContactFlowId=F,Content=re.sub(AG+r':\d+',AG+':17',s))
l.update_function_code(FunctionName='practice-desk-bridge',ZipFile=open(os.path.join(BK,'lambda.pre-smsconsent-20261028.zip'),'rb').read())
l.get_waiter('function_updated_v2').wait(FunctionName='practice-desk-bridge')
print('OK flow -> agent :17, lambda code restored')
