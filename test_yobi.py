import asyncio
import sys
sys.path.insert(0, '/app')
from app.clients.competitor_scraper import scrape_competitor

async def run():
    comp = {'name': 'test', 'url': 'https://novosibirsk.yobidoyobi.ru/', 'parser': 'playwright', 'active': True}
    try:
        items = await scrape_competitor(comp)
        print('Найдено:', len(items), file=sys.stderr)
        for i in items[:25]:
            print(repr(i), file=sys.stderr)
    except Exception as e:
        print('ОШИБКА:', e, file=sys.stderr)
        import traceback; traceback.print_exc(file=sys.stderr)

asyncio.run(run())
