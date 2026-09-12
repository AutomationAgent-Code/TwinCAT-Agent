"""Bounded recovery records shared by legacy and SQLite histories."""
import json
import re
import time


def original_request(request):
    text = str(request or '')
    # Unwrap only our known legacy envelope, not arbitrary user prose.
    for _ in range(20):
        if not text.startswith('继续执行上次') or '原任务：' not in text:
            break
        text = text.split('原任务：', 1)[1]
        text = text.split('\n中断原因：', 1)[0]
    return text[:4000]


def error_category(error):
    text = str(error).lower()
    if '402' in text or 'insufficient_balance' in text:
        return 'billing'
    if 'support image' in text or 'image input' in text:
        return 'model_capability'
    if 'unsupported model' in text or re.search(r'api (400|401|403|404)\b', text):
        return 'provider_configuration'
    if any(word in text for word in ('取消', '停止', 'cancel')):
        return 'cancelled'
    return 'execution_error'


def make_recovery(messages, request, error, partial='', previous=None):
    original = original_request(request)
    if not original and previous:
        original = original_request(previous.get('request'))
    recent = []
    for message in messages[-12:]:
        # Never copy private provider fields, image payloads or recursive envelopes.
        item = {'role': message.get('role'), 'name': message.get('name')}
        if message.get('role') == 'tool':
            result = message.get('result')
            item['result'] = ({k: result[k] for k in
                ('status', 'error', 'written', 'verified', 'next_action') if k in result}
                if isinstance(result, dict) else str(result)[:600])
        else:
            item['text'] = original_request(message.get('text'))[-800:]
            item['tool_names'] = [c.get('name') for c in (message.get('tool_calls') or [])[:10]]
        if len(json.dumps(recent + [item], ensure_ascii=False)) <= 10000:
            recent.append(item)
    return {'request': original, 'error': str(error or '')[:1000],
            'error_category': error_category(error), 'partial': str(partial or '')[-3000:],
            'recent_context': json.dumps(recent, ensure_ascii=False), 'saved_at': time.time()}


def resume_text(recovery, user_text):
    return (
        '继续执行上次未完成的任务。先判断记录中的真实中断原因，不要一律当作网络故障。'
        '余额、模型名称或图像能力问题需要修正配置；不要声称没有上下文，不要重复已经成功的工具操作。\n'
        f"原任务：{original_request(recovery.get('request'))}\n"
        f"中断原因：{recovery.get('error', '')}\n"
        f"中断前输出：{recovery.get('partial', '')}\n"
        f"最近执行上下文：{recovery.get('recent_context', '')}\n用户指令：{user_text}"
    )
