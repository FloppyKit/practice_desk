import boto3, re
c=boto3.client('connect',region_name='us-east-1'); I='e19b711e-6756-4e8b-a486-7407df3c9db5'; F='37247481-2117-4b24-aac2-53bfe71c8634'; AG='b210d774-9a6b-4536-88b0-c8b30e4ed386'
s=c.describe_contact_flow(InstanceId=I,ContactFlowId=F)['ContactFlow']['Content']
c.update_contact_flow_content(InstanceId=I,ContactFlowId=F,Content=re.sub(AG+r':\d+',AG+':15',s)); print('OK flow -> agent :15')
