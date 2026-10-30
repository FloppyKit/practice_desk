"""Crisis alert: lambda (notify_matt urgency=crisis -> caller number + CRISIS prefix), prompt (call notify_matt after 988/911), agent from v18."""
import boto3, copy, io, os, re, urllib.request, zipfile
R='us-east-1'; A='59aa47bc-7bbc-4710-8085-c176a1f5e07c'; AG='b210d774-9a6b-4536-88b0-c8b30e4ed386'; BASE=18
PR='f1e8b4fd-af1a-406c-95e8-1b17896d7529'; FLOW='37247481-2117-4b24-aac2-53bfe71c8634'
INST='e19b711e-6756-4e8b-a486-7407df3c9db5'; SP='11824fcc-7eba-45fa-ad4c-7552dfd40bf7'; FN='practice-desk-bridge'
H=os.path.dirname(os.path.abspath(__file__)); BK=os.path.join(H,'..','voice-backups'); os.makedirs(BK,exist_ok=True)
l=boto3.client('lambda',region_name=R); c=boto3.client('connect',region_name=R); q=boto3.client('qconnect',region_name=R)
flow=c.describe_contact_flow(InstanceId=INST,ContactFlowId=FLOW)['ContactFlow']['Content']
assert f'{AG}:{BASE}' in flow, 'flow is not on agent v18; stop'
open(os.path.join(BK,'flow-nova-sonic.pre-crisis-20261029.json'),'w').write(flow)
open(os.path.join(BK,'lambda.pre-crisis-20261029.zip'),'wb').write(urllib.request.urlopen(l.get_function(FunctionName=FN)['Code']['Location']).read())
p=q.get_ai_prompt(assistantId=A,aiPromptId=PR)['aiPrompt']
open(os.path.join(BK,'prompt.pre-crisis-20261029.txt'),'w').write(p['templateConfiguration']['textFullAIPromptEditTemplateConfiguration']['text'])
# 1. Lambda
b=io.BytesIO(); z=zipfile.ZipFile(b,'w'); z.write(os.path.join(H,'lambda_function.py'),'lambda_function.py'); z.close()
l.update_function_code(FunctionName=FN,ZipFile=b.getvalue()); l.get_waiter('function_updated_v2').wait(FunctionName=FN)
lv=l.publish_version(FunctionName=FN,Description='crisis alert 20261029')['Version']
# 2. prompt + agent
q.update_ai_prompt(assistantId=A,aiPromptId=PR,visibilityStatus=p['visibilityStatus'],
  templateConfiguration={'textFullAIPromptEditTemplateConfiguration':{'text':open(os.path.join(H,'prompt_PracticeDeskVoiceOrchestration.txt')).read()}})
pv=q.create_ai_prompt_version(assistantId=A,aiPromptId=PR)['versionNumber']
g=q.get_ai_agent(assistantId=A,aiAgentId=f'{AG}:{BASE}')['aiAgent']; cfg=copy.deepcopy(g['configuration'])
o=cfg['orchestrationAIAgentConfiguration']; o['orchestrationAIPromptId']=f'{PR}:{pv}'
for t in o['toolConfigurations']:
  if t.get('toolName')=='notify_matt':
    t['inputSchema']['properties']['urgency']['description']='normal, today, urgent_non_emergency, or crisis (after giving 988 / 911)'
q.update_ai_agent(assistantId=A,aiAgentId=AG,visibilityStatus=g['visibilityStatus'],configuration=cfg)
av=q.create_ai_agent_version(assistantId=A,aiAgentId=AG)['versionNumber']
arn=f'arn:aws:wisdom:{R}:256187519760:ai-agent/{A}/{AG}:{av}'
c.associate_security_profiles(InstanceId=INST,SecurityProfiles=[{'Id':SP}],EntityType='AI_AGENT',EntityArn=arn)
assert c.list_entity_security_profiles(InstanceId=INST,EntityType='AI_AGENT',EntityArn=arn)['SecurityProfiles']
new=re.sub(AG+r':\d+',f'{AG}:{av}',flow); assert new!=flow
c.update_contact_flow_content(InstanceId=INST,ContactFlowId=FLOW,Content=new)
print(f'OK lambda v{lv} prompt v{pv} agent v{av} (profile attached) flow -> :{av}')
