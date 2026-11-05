"""
Merchy-AI chatbot Q&A evaluation: test set, baseline and RAG.

Run from the backend folder:
    python eval/chatbot_qa_eval.py build        # draft test set -> data/qa/qa_test_set_draft.csv
    (review the draft, save it as data/qa/qa_test_set_v1.csv)
    python eval/chatbot_qa_eval.py run facts-in-prompt
    python eval/chatbot_qa_eval.py spotcheck facts-in-prompt
    python eval/chatbot_qa_eval.py run rag
    python eval/chatbot_qa_eval.py run real      # needs CHATBOT_URL in .env
    python eval/chatbot_qa_eval.py compare
"""
import argparse, json, os, sys, time
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / 'data'
QA = DATA / 'qa'
QA.mkdir(parents=True, exist_ok=True)
load_dotenv(BACKEND / '.env')

STORES = {'Paul Eagle': 'pauleagleauctions', 'fellers': 'fellerssmallengines'}
MODEL = 'gemini-3.5-flash-lite'
EMBED_MODEL = 'gemini-embedding-001'
JUDGE_VERSION = 'v1'
TOP_K = 4
PER_CATEGORY = 6
PAUSE = 4          # seconds between requests: stays under 15 requests/minute

_client = None
def client():
    global _client
    if _client is None:
        from google import genai
        key = os.getenv('GOOGLE_API_KEY')
        if not key:
            sys.exit('Add GOOGLE_API_KEY=... to backend/.env')
        _client = genai.Client(api_key=key)
    return _client


def ask_gemini(system, payload, retries=4):
    for attempt in range(retries):
        try:
            resp = client().models.generate_content(
                model=MODEL, contents=json.dumps(payload, ensure_ascii=False),
                config={'system_instruction': system, 'temperature': 0,
                        'response_mime_type': 'application/json'})
            return json.loads(resp.text)
        except Exception as e:
            wait = 10 * 2 ** attempt
            print(f'  attempt {attempt + 1} failed ({str(e)[:60]}), retrying in {wait}s')
            time.sleep(wait)
    raise RuntimeError('Gemini call failed; run the command again later')


# ---------- store facts ----------
FACTS_FILE = QA / 'store_facts.json'
FACTS_TEMPLATE = BACKEND / 'eval' / 'store_facts.example.json'

def load_facts():
    if not FACTS_FILE.exists():
        FACTS_FILE.write_text(FACTS_TEMPLATE.read_text())
        sys.exit(f'Created {FACTS_FILE.relative_to(BACKEND)}. Replace every TODO with the real store policy, then run again.')
    facts = json.loads(FACTS_FILE.read_text())
    missing = [k for k, v in facts.items() if str(v).startswith('TODO')]
    if missing:
        sys.exit(f'Fill in these store facts in {FACTS_FILE.relative_to(BACKEND)} first: {missing}')
    return facts

def facts_text(facts):
    return '\n'.join(f'- {k}: {v}' for k, v in facts.items())


# ---------- build: questions + draft answer keys ----------
REWRITE_SYSTEM = '''You help build a test set for a customer-support chatbot of an eBay store that sells motorcycle and small-engine parts.
Each input is real buyer feedback left after a problem. Rewrite each one as the message that buyer would have sent the seller BEFORE leaving feedback, asking for help.
Keep the buyer's situation and details. Remove names, order numbers and contact details. Write like a real buyer: short and informal, one to three sentences.
Reply with only a JSON object mapping each id to the question.'''

PRESALE_SYSTEM = '''You help build a test set for a customer-support chatbot of an eBay store that sells motorcycle and small-engine parts.
For each listing, write two short, realistic questions a buyer might send before buying: for example fit for a specific vehicle, condition, what is included, or shipping time.
Reply with only a JSON object mapping each id to a list of two questions.'''

def key_system(facts):
    return f'''You write the answer key for a customer-support test set of an eBay motorcycle and small-engine parts store.
Store facts (the only source of truth):
{facts_text(facts)}

For each buyer question, give:
- key_points: 2 to 3 short points a correct reply must contain, based only on the store facts. If the facts do not answer it, one key point should be that staff will follow up.
- escalate: true if a person must act (refund, return label, damaged, wrong or missing item, lost package claim) or the facts do not cover the question; otherwise false.
Reply with only a JSON object mapping each id to {{"key_points": [...], "escalate": true or false}}.'''

def load_feedback():
    frames = []
    for store, folder in STORES.items():
        d = pd.read_csv(DATA / folder / 'feedback_clean.csv', dtype={'feedback_id': str})
        d['store'] = store
        d['uid'] = store + '-' + d['feedback_id']
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    for name in ['feedback_labels_v2.csv', 'feedback_labels_llm_both.csv']:
        found = sorted(DATA.rglob(name))
        if found:
            labels = pd.read_csv(found[0], dtype=str)
            break
    else:
        sys.exit('No labels file. Download feedback_labels_v2.csv (or feedback_labels_llm_both.csv) '
                 'from the merchy-ai Drive folder into backend/data/')
    col = 'label' if 'label' in labels.columns else 'llm_label'
    labels = labels[['uid', col]].drop_duplicates('uid').rename(columns={col: 'label'})
    df = df.merge(labels, on='uid', how='inner')
    print(len(df), 'labeled comments from', found[0].name)
    return df, found[0].name

def load_listings():
    path = QA / 'listings.csv'
    if not path.exists():
        return []
    lst = pd.read_csv(path, dtype=str).fillna('')
    title_col = next((c for c in lst.columns if c.lower() == 'title'), lst.columns[0])
    out = []
    for r in lst.to_dict('records'):
        details = ' '.join(f'{c}: {r[c]}' for c in lst.columns if c != title_col and r[c])
        out.append(f'{r[title_col]}. {details}'[:1200])
    return out

def build():
    facts = load_facts()
    df, labels_source = load_feedback()
    complaints = df[(df['label'] != 'positive') & (df['comment_text'].str.len() >= 25)]
    sample = complaints.sample(frac=1, random_state=42).groupby('label').head(PER_CATEGORY)
    print('Sampled complaints:', sample['label'].value_counts().to_dict())

    rewritten = ask_gemini(REWRITE_SYSTEM, {r.uid: r.comment_text for r in sample.itertuples()})
    rows = [{'source': 'feedback', 'source_id': r.uid, 'category': r.label,
             'source_text': r.comment_text, 'question': rewritten.get(r.uid, '')}
            for r in sample.itertuples() if rewritten.get(r.uid)]

    listings = load_listings()[:8]
    if listings:
        time.sleep(PAUSE)
        items = {f'listing-{i}': t for i, t in enumerate(listings)}
        out = ask_gemini(PRESALE_SYSTEM, items)
        rows += [{'source': 'listing', 'source_id': k, 'category': 'pre_sale', 'source_text': items[k], 'question': q}
                 for k, qs in out.items() if k in items for q in qs[:2]]
    else:
        print('No data/qa/listings.csv: skipping pre-sale questions')

    q = pd.DataFrame(rows)
    q.insert(0, 'qid', [f'q{i:03d}' for i in range(len(q))])
    keys = {}
    for start in range(0, len(q), 10):
        time.sleep(PAUSE)
        part = q.iloc[start:start + 10]
        keys.update(ask_gemini(key_system(facts), {r.qid: r.question for r in part.itertuples()}))
    q['key_points'] = q['qid'].map(lambda i: ' | '.join(keys.get(i, {}).get('key_points', [])))
    q['escalate'] = q['qid'].map(lambda i: bool(keys.get(i, {}).get('escalate', False)))
    q['keep'] = 'Y'
    q.to_csv(QA / 'qa_test_set_draft.csv', index=False)
    print(f'\nSaved {len(q)} questions to data/qa/qa_test_set_draft.csv')
    print('Review it (fix questions, key_points separated by " | ", escalate, keep=Y/N),')
    print('then save the reviewed file as data/qa/qa_test_set_v1.csv')


# ---------- run: answer, grade, log ----------
ANSWER_PROMPT = '''You are the customer-support assistant for an eBay store that sells motorcycle and small-engine parts.
Answer the buyer using only the store information below. If it does not cover the question, say a team member will follow up.
Store information:
{context}'''

def judge_system(facts):
    return f'''You grade replies from the customer-support chatbot of an eBay motorcycle and small-engine parts store.
Store facts:
{facts_text(facts)}

You get a buyer question, the answer key points, and the chatbot reply.
- points: for each key point, in order, true if the reply covers it (same meaning, wording can differ), else false.
- escalated: true if the reply hands the buyer to a person or says staff will follow up or take action.
- unsupported_claim: true if the reply states a policy, time, price or promise that contradicts or is not in the store facts or the listing details.
- reason: one short sentence.
Reply with only JSON: {{"points": [...], "escalated": true or false, "unsupported_claim": true or false, "reason": "..."}}'''

def load_test():
    path = QA / 'qa_test_set_v1.csv'
    if not path.exists():
        sys.exit('Review data/qa/qa_test_set_draft.csv and save it as data/qa/qa_test_set_v1.csv first')
    t = pd.read_csv(path, dtype=str).fillna('')
    t = t[t['keep'].str.upper().str.strip() == 'Y'].copy()
    t['key_points'] = t['key_points'].map(lambda s: [p.strip() for p in s.split('|') if p.strip()])
    t['escalate'] = t['escalate'].str.upper().str.strip().isin(['TRUE', '1', 'Y', 'YES'])
    if not (t['key_points'].map(len) > 0).all():
        sys.exit('Every kept question needs at least one key point')
    return t

def generate_answer(question, context):
    resp = client().models.generate_content(model=MODEL, contents=question,
        config={'system_instruction': ANSWER_PROMPT.format(context=context), 'temperature': 0})
    return resp.text

def embed(texts, task):
    from google.genai import types
    vecs = []
    for start in range(0, len(texts), 100):
        for attempt in range(4):
            try:
                res = client().models.embed_content(model=EMBED_MODEL, contents=texts[start:start + 100],
                                                    config=types.EmbedContentConfig(task_type=task))
                break
            except Exception as e:
                wait = 10 * 2 ** attempt
                print(f'  embedding attempt {attempt + 1} failed ({str(e)[:60]}), retrying in {wait}s')
                time.sleep(wait)
        else:
            raise RuntimeError('Embedding failed; run the command again later')
        vecs += [e.values for e in res.embeddings]
    v = np.array(vecs)
    return v / np.linalg.norm(v, axis=1, keepdims=True)

def make_answer_fn(config, facts, test):
    if config == 'facts-in-prompt':
        return (lambda q: generate_answer(q, facts_text(facts))), {
            'answer_model': MODEL, 'knowledge': 'store facts', 'retrieval': 'none'}
    if config == 'rag':
        chunks = [f'{k.replace("_", " ")}: {v}' for k, v in facts.items()] + load_listings()
        n_listings = len(chunks) - len(facts)
        print(f'Knowledge base: {len(chunks)} chunks ({n_listings} listings)')
        kb_vecs = embed(chunks, 'RETRIEVAL_DOCUMENT')
        q_vecs = dict(zip(test['question'], embed(test['question'].tolist(), 'RETRIEVAL_QUERY')))
        def retrieve(q):
            v = q_vecs.get(q)
            v = embed([q], 'RETRIEVAL_QUERY')[0] if v is None else v
            return [chunks[i] for i in np.argsort(-(kb_vecs @ v))[:TOP_K]]
        for q in test['question'].head(2):
            print('Retrieval check:', q)
            for c in retrieve(q):
                print('   ->', c[:110])
        return (lambda q: generate_answer(q, '\n'.join(f'- {c}' for c in retrieve(q)))), {
            'answer_model': MODEL, 'knowledge': f'store facts + {n_listings} listings',
            'retrieval': EMBED_MODEL, 'top_k': TOP_K}
    if config == 'real':
        import requests
        url = os.getenv('CHATBOT_URL')
        if not url:
            sys.exit('Add CHATBOT_URL=... to backend/.env (local: http://127.0.0.1:<port>/<chat route>)')
        req_field = os.getenv('CHATBOT_REQUEST_FIELD', 'message')
        resp_field = os.getenv('CHATBOT_RESPONSE_FIELD', 'reply')
        def ask(q):
            for attempt in range(3):
                try:
                    r = requests.post(url, json={req_field: q}, timeout=90)
                    r.raise_for_status()
                    return r.json()[resp_field]
                except Exception as e:
                    print(f'  chatbot attempt {attempt + 1} failed ({str(e)[:60]})')
                    time.sleep(10)
            return ''
        return ask, {'answer_model': 'app', 'knowledge': 'app', 'retrieval': 'none'}
    sys.exit(f'Unknown config {config}')

def run_name(config):
    return 'baseline-real-chatbot' if config == 'real' else config

def collect(name, answer_fn, test):
    path = QA / f'replies_{name}.csv'
    done = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame(columns=['qid', 'reply', 'latency_s'])
    todo = test[~test['qid'].isin(done['qid'])]
    print(f'{name}: {len(todo)} questions left')
    for i, r in enumerate(todo.itertuples(), 1):
        t0 = time.time()
        try:
            reply = answer_fn(r.question)
        except Exception as e:
            print('  stopped:', str(e)[:80], '- run the command again later'); break
        done = pd.concat([done, pd.DataFrame([{'qid': r.qid, 'reply': reply,
                                               'latency_s': round(time.time() - t0, 2)}])], ignore_index=True)
        done.to_csv(path, index=False)
        print(f'  answered {i}/{len(todo)}', end='\r')
        time.sleep(PAUSE)
    print()

def grade(name, facts, test):
    path = QA / f'graded_{name}.csv'
    replies = pd.read_csv(QA / f'replies_{name}.csv', dtype=str).fillna('')
    old = pd.read_csv(path, dtype={'qid': str}) if path.exists() else pd.DataFrame(columns=['qid'])
    rows, seen = old.to_dict('records'), set(old['qid'])
    system = judge_system(facts)
    for r in test.merge(replies, on='qid').itertuples():
        if r.qid in seen:
            continue
        g = ask_gemini(system, {'question': r.question, 'key_points': r.key_points, 'reply': r.reply})
        pts = [bool(p) for p in g.get('points', [])][:len(r.key_points)]
        pts += [False] * (len(r.key_points) - len(pts))
        rows.append({'qid': r.qid, 'points_covered': sum(pts), 'points_total': len(pts),
                     'escalated': bool(g.get('escalated')), 'unsupported_claim': bool(g.get('unsupported_claim')),
                     'reason': g.get('reason', '')})
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f'  graded {len(rows)}/{len(test)}', end='\r')
        time.sleep(PAUSE)
    print()
    return scored(name, test)

def scored(name, test):
    replies = pd.read_csv(QA / f'replies_{name}.csv', dtype=str).fillna('')
    gr = test.merge(replies, on='qid').merge(pd.read_csv(QA / f'graded_{name}.csv', dtype={'qid': str}), on='qid')
    for col in ['escalated', 'unsupported_claim']:
        gr[col] = gr[col].astype(str).str.lower() == 'true'
    gr['escalation_ok'] = gr['escalated'] == gr['escalate']
    gr['correct'] = ((gr['points_covered'] == gr['points_total']) & gr['escalation_ok']
                     & ~gr['unsupported_claim'] & (gr['reply'] != ''))
    m = {'reply_accuracy': gr['correct'].mean(),
         'key_point_coverage': gr['points_covered'].sum() / gr['points_total'].sum(),
         'escalation_accuracy': gr['escalation_ok'].mean(),
         'unsupported_rate': gr['unsupported_claim'].mean(),
         'median_latency_s': pd.to_numeric(gr['latency_s']).median()}
    return gr, m

def log_run(name, params, metrics, test):
    import mlflow
    mlflow.set_tracking_uri((DATA / 'mlruns').resolve().as_uri())
    mlflow.set_experiment('chatbot-qa')
    with mlflow.start_run(run_name=name):
        mlflow.log_params({'test_set': 'qa_test_set_v1.csv', 'test_size': len(test), 'judge_model': MODEL,
                           'judge_version': JUDGE_VERSION, 'store_facts': 'store_facts.json', **params})
        mlflow.log_metrics({k: round(float(v), 4) for k, v in metrics.items()})
        for f in [QA / f'graded_{name}.csv', QA / 'qa_test_set_v1.csv', FACTS_FILE]:
            mlflow.log_artifact(str(f))
    print('Logged to MLflow. Browse with: mlflow ui --backend-store-uri data/mlruns')

def run(config):
    facts, test = load_facts(), load_test()
    name = run_name(config)
    answer_fn, params = make_answer_fn(config, facts, test)
    collect(name, answer_fn, test)
    if len(pd.read_csv(QA / f'replies_{name}.csv')) < len(test):
        sys.exit('Not all questions answered yet; run the same command again later')
    gr, m = grade(name, facts, test)
    if len(gr) < len(test):
        sys.exit('Not all replies graded yet; run the same command again later')
    print(pd.Series(m).round(3).to_string())
    print('\nBy category:\n', gr.groupby('category')['correct'].agg(['mean', 'count']).round(2).to_string())
    log_run(name, params, m, test)


# ---------- spot-check the judge ----------
def spotcheck(config):
    test = load_test()
    gr, _ = scored(run_name(config), test)
    spot = gr.sample(min(10, len(gr)), random_state=0)
    agree = 0
    for r in spot.itertuples():
        print('\n' + '=' * 70)
        print('Q:', r.question)
        print('Key points:', r.key_points, '| should escalate:', r.escalate)
        print('Reply:', r.reply[:800])
        print(f'Judge: {r.points_covered}/{r.points_total} points, escalated={r.escalated}, '
              f'unsupported={r.unsupported_claim}, correct={r.correct} ({r.reason})')
        agree += input('Agree with the judge? [y/n] ').strip().lower().startswith('y')
    result = {'run': run_name(config), 'agree': agree, 'checked': len(spot), 'agreement': agree / len(spot)}
    (QA / 'judge_spotcheck.json').write_text(json.dumps(result, indent=2))
    print(f'\nJudge agreement: {agree}/{len(spot)} saved to data/qa/judge_spotcheck.json')


# ---------- compare ----------
def compare():
    test = load_test()
    summary, by_cat = {}, {}
    for name in ['baseline-real-chatbot', 'facts-in-prompt', 'rag']:
        if (QA / f'graded_{name}.csv').exists():
            gr, m = scored(name, test)
            summary[name], by_cat[name] = m, gr.groupby('category')['correct'].mean()
    if not summary:
        sys.exit('No graded runs yet')
    print(pd.DataFrame(summary).T.round(3).to_string())
    print('\nReply accuracy by category:\n', pd.DataFrame(by_cat).round(2).to_string())
    spot = QA / 'judge_spotcheck.json'
    if spot.exists():
        s = json.loads(spot.read_text())
        print(f"\nJudge agreement (spot-check on {s['run']}): {s['agree']}/{s['checked']}")
    print(f"\nTest set: {len(test)} questions ({(test.source == 'feedback').sum()} from feedback, "
          f"{(test.source == 'listing').sum()} from listings)")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Merchy-AI chatbot Q&A evaluation')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('build')
    for c in ['run', 'spotcheck']:
        p = sub.add_parser(c)
        p.add_argument('config', choices=['facts-in-prompt', 'rag', 'real'])
    sub.add_parser('compare')
    a = ap.parse_args()
    if a.cmd == 'build':
        build()
    elif a.cmd == 'compare':
        compare()
    elif a.cmd == 'run':
        run(a.config)
    else:
        spotcheck(a.config)
