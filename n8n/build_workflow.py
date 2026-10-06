"""Собирает n8n-сценарий «Форма сайта → Битрикс24 без дублей» → n8n/workflow.json.

Секрет (адрес вебхука Битрикс24) в сценарии НЕ хранится: узлы берут его из окружения n8n ($env.B24_WEBHOOK),
поэтому workflow.json можно выкладывать в открытый репозиторий.
Сценарий рассчитан на ПРОСТОЙ режим CRM (контакт + сделка, по умолчанию у новых порталов); на классическом
режиме он отвечает понятной ошибкой 501, а не создаёт дубли молча. Обе ветки — в боте (crm.py).
"""
import json
import uuid
from pathlib import Path

B24 = "{{ $env.B24_WEBHOOK }}"
PORTAL = "$env.B24_WEBHOOK.split('/rest/')[0]"
LEAD = "$('Проверка и нормализация').item.json"
nodes, connections = [], {}


def node(name, ntype, version, params, pos, **extra):
    nodes.append({"id": str(uuid.uuid4()), "name": name, "type": ntype, "typeVersion": version,
                  "position": pos, "parameters": params, **extra})
    return name


def b24(name, method, body_js, pos):
    """Запрос к REST Битрикс24 с автоповтором (таймаут, лимит запросов, техработы)."""
    return node(name, "n8n-nodes-base.httpRequest", 4.2, {
        "method": "POST", "url": f"={B24}{method}.json",
        "sendBody": True, "specifyBody": "json", "jsonBody": f"={{{{ JSON.stringify({body_js}) }}}}",
        "options": {"timeout": 8000},  # Битрикс24 отвечает за <1 с; зависшее соединение (бывает на нестабильной сети) рвём и повторяем
    }, pos, retryOnFail=True, maxTries=5, waitBetweenTries=1000)


def if_node(name, left, op, pos, right=None):
    cond = {"id": "c1", "leftValue": left, "operator": op}
    if right is not None:
        cond["rightValue"] = right
    return node(name, "n8n-nodes-base.if", 2.2, {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
        "conditions": [cond], "combinator": "and"}, "options": {}}, pos)


def respond(name, code, body_js, pos):
    return node(name, "n8n-nodes-base.respondToWebhook", 1.1, {
        "respondWith": "json", "responseBody": f"={{{{ JSON.stringify({body_js}) }}}}",
        "options": {"responseCode": code}}, pos)


def link(src, dst, branch=0):
    outs = connections.setdefault(src, {"main": []})["main"]
    while len(outs) <= branch:
        outs.append([])
    outs[branch].append({"node": dst, "type": "main", "index": 0})


TRUE = {"type": "boolean", "operation": "true", "singleValue": True}

wh = node("Заявка с сайта", "n8n-nodes-base.webhook", 2,
          {"httpMethod": "POST", "path": "lead-to-b24", "responseMode": "responseNode", "options": {}},
          [0, 0], webhookId=str(uuid.uuid4()))
norm = node("Проверка и нормализация", "n8n-nodes-base.code", 2, {"jsCode": r"""// Телефон в любом формате → +7XXXXXXXXXX (то же правило, что crm.normalize_phone в боте)
const b = $input.first().json.body || {};
const raw = String(b.phone ?? '');
const d = raw.replace(/\D/g, '');
let phone = null;
if (d.length === 11 && '78'.includes(d[0])) phone = '+7' + d.slice(1);
else if (d.length === 10 && d[0] === '9') phone = '+7' + d;
else if (raw.trim().startsWith('+') && d.length >= 11 && d.length <= 15 && !d.startsWith('7')) phone = '+' + d;
const need = String(b.need ?? '').trim().slice(0, 1000) || 'Заявка с сайта';
return [{ json: { name: String(b.name ?? '').trim().slice(0, 60) || 'Без имени', phone, need } }];"""}, [220, 0])
ok_phone = if_node("Телефон верный?", "={{ !!$json.phone }}", TRUE, [440, 0])
bad = respond("Ответ 422", 422, "{ status: 'error', error: 'Неверный номер телефона' }", [660, 180])
mode = b24("Режим CRM", "crm.settings.mode.get", "{}", [660, 0])
simple = if_node("Простой режим?", "={{ Number($json.result) }}", {"type": "number", "operation": "equals"}, [880, 0], right=2)
classic = respond("Ответ 501", 501, "{ status: 'error', error: 'Портал в классическом режиме (лиды): этот сценарий для простого режима — используйте ветку лидов (bitrix24-leads/crm.py)' }", [1100, 180])
find = b24("Поиск клиента по телефону", "crm.duplicate.findbycomm",
           f"{{ type: 'PHONE', values: [{LEAD}.phone] }}", [1100, 0])
known = if_node("Клиент известен?", "={{ Array.isArray($json.result?.CONTACT) && $json.result.CONTACT.length > 0 }}",
                TRUE, [1320, 0])
CONTACT_ID = "Math.max(...$('Поиск клиента по телефону').item.json.result.CONTACT)"
open_deals = b24("Открытые сделки клиента", "crm.deal.list",
                 f"{{ filter: {{ CONTACT_ID: {CONTACT_ID}, STAGE_SEMANTIC_ID: 'P' }}, order: {{ ID: 'DESC' }}, select: ['ID'] }}",
                 [1540, -120])
has_open = if_node("Есть открытая сделка?", "={{ ($json.result || []).length > 0 }}", TRUE, [1760, -120])
comment = b24("Комментарий в открытую сделку", "crm.timeline.comment.add",
              f"{{ fields: {{ ENTITY_ID: $json.result[0].ID, ENTITY_TYPE: 'deal', COMMENT: 'Повторная заявка (сайт):\\n' + {LEAD}.need }} }}",
              [1980, -240])
r_attached = respond("Ответ: дополнили открытую", 200,
                     f"{{ status: 'attached', deal_id: Number($('Открытые сделки клиента').item.json.result[0].ID), url: {PORTAL} + '/crm/deal/details/' + $('Открытые сделки клиента').item.json.result[0].ID + '/' }}",
                     [2200, -240])
repeat_deal = b24("Сделка: повторное обращение", "crm.deal.add",
                  f"{{ fields: {{ TITLE: 'Повторное обращение: ' + {LEAD}.need.slice(0, 60), CONTACT_ID: {CONTACT_ID}, SOURCE_ID: 'WEB', COMMENTS: {LEAD}.need, UF_CRM_NEED: {LEAD}.need }} }}",
                  [1980, -40])
r_repeat = respond("Ответ: повторное обращение", 200,
                   f"{{ status: 'repeat', deal_id: Number($json.result), url: {PORTAL} + '/crm/deal/details/' + $json.result + '/' }}",
                   [2200, -40])
new_contact = b24("Новый контакт", "crm.contact.add",
                  f"{{ fields: {{ NAME: {LEAD}.name, PHONE: [{{ VALUE: {LEAD}.phone, VALUE_TYPE: 'WORK' }}], SOURCE_ID: 'WEB' }} }}",
                  [1540, 140])
new_deal = b24("Новая сделка", "crm.deal.add",
               f"{{ fields: {{ TITLE: 'Заявка с сайта: ' + {LEAD}.need.slice(0, 60), CONTACT_ID: Number($json.result), SOURCE_ID: 'WEB', COMMENTS: {LEAD}.need, UF_CRM_NEED: {LEAD}.need }} }}",
               [1760, 140])
r_created = respond("Ответ: новая заявка", 200,
                    f"{{ status: 'created', deal_id: Number($json.result), url: {PORTAL} + '/crm/deal/details/' + $json.result + '/' }}",
                    [1980, 140])

link(wh, norm)
link(norm, ok_phone)
link(ok_phone, mode, 0); link(ok_phone, bad, 1)
link(mode, simple)
link(simple, find, 0); link(simple, classic, 1)
link(find, known)
link(known, open_deals, 0); link(known, new_contact, 1)
link(open_deals, has_open)
link(has_open, comment, 0); link(has_open, repeat_deal, 1)
link(comment, r_attached)
link(repeat_deal, r_repeat)
link(new_contact, new_deal)
link(new_deal, r_created)

wf = {"id": "b24LeadsFromSite", "name": "Форма сайта → Битрикс24 без дублей", "active": False,
      "settings": {"executionOrder": "v1"}, "nodes": nodes, "connections": connections}
out = Path(__file__).parent / "workflow.json"
out.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{out}: {len(nodes)} узлов")
