"""Opt-in live DeepSeek validation against public websites; no test runner provided.

Run with PYTHONPATH=src python scripts/live_web_validation.py --env-file /path/to/.env
Only the report and public page evidence are persisted, never API credentials.
"""

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
import re
from pathlib import Path
import time
from urllib.parse import urlsplit, parse_qs

from dotenv import dotenv_values

from e2e_web import AgentConfig, CacheConfig, DeepSeekExecutor, ObservationConfig, Web


def settings(path):
    if path and not Path(path).is_file():
        raise FileNotFoundError(path)
    values = dotenv_values(path, encoding='utf-8-sig') if path else {}
    values.update(os.environ)
    return values


class MeteredExecutor:
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def complete(self, request, *, timeout):
        start = time.monotonic()
        item = dict(kind=request['kind'], vision=bool(request.get('screenshot')))
        self.calls.append(item)
        try:
            answer = self.inner.complete(request, timeout=timeout)
            item['status'] = 'passed'
            item['decision'] = answer
            return answer
        except Exception as exc:
            item.update(status='failed', code=getattr(exc, 'code', type(exc).__name__))
            raise
        finally:
            item['seconds'] = round(time.monotonic() - start, 3)


def cases():
    return [
        dict(name='python-navigation', url='https://www.python.org/',
             task='Open the Downloads page using the website navigation. Do not download any files. Finish when the main Python downloads page is visible.',
             expected='The current page is the Python downloads page and shows Python download information.',
             path='/downloads', extract='Extract the main heading on the current page.'),
        dict(name='pypi-navigation', url='https://pypi.org/project/requests/',
             task='Open the Release history section for the requests package using the page navigation. Do not leave this project.',
             expected='The requests project Release history section is visible on PyPI.',
             path='/project/requests', extract='Extract the name of the package and one visible release version.'),
        dict(name='docs-search', url='https://docs.python.org/3/',
             task='Use the documentation search box to search for asyncio. Submit the search and wait until actual search results are visible. Do not open a result.',
             expected='The documentation search results for asyncio are visible and include at least one result.',
             path='/search.html', extract='Extract the title of the first search result for asyncio.'),
        dict(name='visual-checkbox', url='https://the-internet.herokuapp.com/checkboxes',
             task='Use click_at with coordinates from the current screenshot to check the first checkbox if unchecked. Do not use check, CSS or locator clicks. Leave the second checkbox checked. Finish when both are checked.',
             expected='Both checkboxes are checked.', path='/checkboxes',
             extract='Describe whether the first and second checkboxes are checked.'),
        dict(name='public-form', url='https://the-internet.herokuapp.com/dropdown',
             task='Select Option 2 from the dropdown and finish when Option 2 is selected.',
             expected='The dropdown currently has Option 2 selected.', path='/dropdown',
             extract='Extract the currently selected dropdown option.'),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    parser.add_argument('--model')
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--vision', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    config = settings(args.env_file)
    key = config.get('DEEPSEEK_API_KEY') or config.get('LLM_API_KEY')
    model_name = args.model or config.get('DEEPSEEK_MODEL') or config.get('LLM_MODEL')
    base = config.get('DEEPSEEK_BASE_URL') or config.get('LLM_BASE_URL') or 'https://api.deepseek.com'
    if not key or not model_name:
        raise SystemExit('Configure a DeepSeek API key and explicit model name.')
    if urlsplit(base).hostname != 'api.deepseek.com':
        raise SystemExit('This validation script sends existing credentials only to api.deepseek.com.')
    output = Path(args.output or '.e2e/live/' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True, exist_ok=True)
    executor = MeteredExecutor(DeepSeekExecutor(model=model_name, api_key=key, base_url=base,
                              timeout=60, thinking=False, supports_vision=args.vision,
                              request_options={'max_tokens': 2048}))
    report = dict(model=model_name, host=urlsplit(base).hostname, vision=args.vision, cases=[])
    selected = [case for case in cases() if (not args.case and case['name'] != 'visual-checkbox') or case['name'] in args.case]
    if not selected:
        raise SystemExit('No matching cases.')
    if any(case['name'] == 'visual-checkbox' for case in selected) and not args.vision:
        raise SystemExit('The visual-checkbox case requires --vision.')
    with Web(executable_path=config.get('E2E_CHROMIUM_EXECUTABLE') or '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome') as web:
        for case in selected:
            start = time.monotonic()
            row = dict(name=case['name'], url=case['url'], status='running')
            report['cases'].append(row)
            print(json.dumps(dict(event='start', case=case['name']), ensure_ascii=False), flush=True)
            call_start = len(executor.calls)
            try:
                with web.session(name=case['name'], app_identity=urlsplit(case['url']).hostname,
                                 executor=executor, cache=CacheConfig(mode='off'),
                                 observation=ObservationConfig(max_nodes=350, max_elements=15000, max_bytes=100000),
                                 agent=AgentConfig(timeout=180, judgment_timeout=60, max_actions=12, max_model_calls=16),
                                 timeout=300, artifacts_dir=output/case['name']) as session:
                    session.page.goto(case['url'], wait_until='domcontentloaded', timeout=45000)
                    session.screenshot(output/case['name']/'before.png')
                    try:
                        result = session.agent.act(case['task'], vision=args.vision)
                        row['act'] = asdict(result)
                        if case['name'] == 'docs-search':
                            session.agent.wait_for('At least one actual asyncio search result is visible; loading has finished.', timeout=45, max_model_calls=6)
                        session.agent.assert_(case['expected'], vision=args.vision)
                        session.verify(lambda: urlsplit(session.page.url).hostname == urlsplit(case['url']).hostname
                                       and case['path'] in urlsplit(session.page.url).path, label='Expected website and path')
                        if case['name'] == 'public-form':
                            session.expect(session.page.locator('select')).to_have_value('2')
                        elif case['name'] == 'pypi-navigation':
                            session.expect(session.page.get_by_role('heading', name=re.compile('^Release history'))).to_be_visible()
                        elif case['name'] == 'docs-search':
                            session.verify(lambda: parse_qs(urlsplit(session.page.url).query).get('q') == ['asyncio'], label='Exact search query')
                            session.expect(session.page.locator('ul.search > li').first).to_be_visible()
                        elif case['name'] == 'visual-checkbox':
                            session.expect(session.page.get_by_role('checkbox').nth(0)).to_be_checked()
                            session.expect(session.page.get_by_role('checkbox').nth(1)).to_be_checked()
                        else:
                            session.expect(session.page.get_by_role('heading', name=re.compile(r'Download the latest version for (macOS|Windows|Linux)'))).to_be_visible()
                        row['extraction'] = session.agent.extract(case['extract'], schema={'type':'object','properties':{'text':{'type':'string','minLength':1}},'required':['text'],'additionalProperties':False}, vision=args.vision)
                        session.screenshot(output/case['name']/'after.png')
                        row.update(status='passed', final_url=session.page.url)
                    finally:
                        if row['status'] != 'passed':
                            try:
                                session.screenshot(output/case['name']/'failure.png')
                            except Exception:
                                pass
            except Exception as exc:
                # SDK errors deliberately omit provider response bodies and credentials.
                row.update(status='failed', code=getattr(exc,'code',type(exc).__name__), message=str(exc)[:1200])
            row['seconds'] = round(time.monotonic()-start,3)
            row['model_calls'] = executor.calls[call_start:]
            (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(row, ensure_ascii=False), flush=True)
    print('Report:', str((output/'report.json').resolve()), flush=True)
    return 0 if all(row['status']=='passed' for row in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
