"""Narrow Anthropic Messages JSON -> fixed Pi Responses bridge, no agent loop."""
import hashlib
import re

from .host import canonical
from .model_policy import MODEL, TOOLS
from .model_probe import strict_json
from .provider_policy import ROUTES


def parse_provider_response(raw, profile):
    value = strict_json(raw)
    if not isinstance(value,dict) or value.get('type') != 'message' or value.get('role') != 'assistant' or value.get('model') not in ROUTES[profile.provider]['aliases'] or not isinstance(value.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,192}',value['id']):
        raise ValueError('provider response identity')
    content = value.get('content')
    if not isinstance(content,list) or not 1 <= len(content) <= 16:
        raise ValueError('provider response content')
    calls, texts, identifiers = [], [], set()
    for block in content:
        if not isinstance(block,dict):
            raise ValueError('invalid block')
        if block.get('type') == 'tool_use':
            if set(block) != {'type','id','name','input'} or not isinstance(block['id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',block['id']) or block['id'] in identifiers or not isinstance(block['name'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',block['name']) or not isinstance(block['input'],dict):
                raise ValueError('invalid tool block')
            identifiers.add(block['id'])
            calls.append(block)
        elif block.get('type') == 'text':
            if set(block) != {'type','text'} or not isinstance(block['text'],str):
                raise ValueError('invalid text block')
            texts.append(block['text'])
        else:
            # First acceptance explicitly disables thinking and all server tools/media.
            raise ValueError('ungranted response capability')
    reason = value.get('stop_reason')
    if (calls and (reason != 'tool_use' or len(calls) != 1)) or (not calls and (reason != 'end_turn' or not ''.join(texts).strip())):
        raise ValueError('incomplete or parallel response')
    usage = value.get('usage')
    if usage is not None:
        fields = ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')
        if not isinstance(usage,dict) or any(type(usage.get(key,0)) is not int or usage.get(key,0) < 0 for key in fields) or not {'input_tokens','output_tokens'}.issubset(usage):
            raise ValueError('invalid usage')
    return value


def normalized_response(value):
    identity = hashlib.sha256(value['id'].encode('utf-8')).hexdigest()[:24]
    calls = [block for block in value['content'] if block['type'] == 'tool_use']
    if calls:
        call = calls[0]
        call_id = hashlib.sha256(call['id'].encode('utf-8')).hexdigest()[:24]
        item = dict(type='function_call',id='fc_'+call_id,call_id='call_'+call_id,name=call['name'],arguments=canonical(call['input']),status='completed')
    else:
        item = dict(type='message',id='msg_'+identity,role='assistant',status='completed',content=[dict(type='output_text',text=''.join(block['text'] for block in value['content']),annotations=[])])
    original = value.get('usage')
    usage = None if original is None else dict(input_tokens=sum(original.get(key,0) for key in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens')),output_tokens=original['output_tokens'])
    response = dict(id='resp_'+identity,model=MODEL,status='completed',output=[item],usage=usage)
    events = [dict(type='response.created',response=dict(id=response['id'],model=MODEL,status='in_progress')),
              dict(type='response.output_item.added',output_index=0,item={**item,**({'arguments':''} if calls else {'content':[]})})]
    if calls:
        events.extend([dict(type='response.function_call_arguments.delta',output_index=0,item_id=item['id'],delta=item['arguments']),
                       dict(type='response.function_call_arguments.done',output_index=0,item_id=item['id'],arguments=item['arguments'])])
    else:
        events.append(dict(type='response.output_text.delta',output_index=0,content_index=0,delta=item['content'][0]['text']))
    events.extend([dict(type='response.output_item.done',output_index=0,item=item),dict(type='response.completed',response=response)])
    body = ''.join('data: '+canonical(event)+'\n\n' for event in events).encode('utf-8')
    return body,response


def remote_tools():
    return [dict(name=tool['name'],description=tool['description'],input_schema=tool['parameters']) for tool in TOOLS]
