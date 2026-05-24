from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
import urllib.request
import re
import html
import json
import time
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright
import os


def get_crypto_prices(symbol, time_iso):
    # 1. Thử lấy giá từ Binance
    binance_sym = symbol.upper() + "USDT"
    dt = datetime.fromisoformat(time_iso)
    ts_ms = int(dt.timestamp() * 1000)

    hist_url = f"https://api.binance.com/api/v3/klines?symbol={binance_sym}&interval=1m&startTime={ts_ms}&limit=1"
    curr_url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_sym}"

    hist_price = None
    curr_price = None

    # Retry logic cho Binance
    for _ in range(2):
        try:
            req = urllib.request.Request(hist_url)
            data = json.loads(urllib.request.urlopen(req, timeout=5).read())
            if data:
                hist_price = float(data[0][1])
                break
        except Exception:
            time.sleep(1)

    for _ in range(2):
        try:
            req = urllib.request.Request(curr_url)
            data = json.loads(urllib.request.urlopen(req, timeout=5).read())
            if 'price' in data:
                curr_price = float(data['price'])
                break
        except Exception:
            time.sleep(1)

    # 2. Nếu không có giá hiện tại trên Binance, thử lấy từ DexScreener
    if curr_price is None:
        try:
            url = f"https://api.dexscreener.com/latest/dex/search?q={symbol.upper()}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if data.get('pairs'):
                # Chọn cặp có thanh khoản (liquidity) cao nhất để tránh coin rác/scam trùng tên
                valid_pairs = [p for p in data['pairs'] if p.get('liquidity', {}).get('usd', 0) > 0]
                if valid_pairs:
                    best_pair = max(valid_pairs, key=lambda p: p['liquidity']['usd'])
                    curr_price = float(best_pair['priceUsd'])
                else:
                    best_pair = data['pairs'][0]
                    curr_price = float(best_pair['priceUsd'])
        except Exception:
            pass

    return hist_price, curr_price


# Bộ nhớ tạm để tránh bị CoinGecko chặn (Cache) - Lưu vào file để không bị mất khi restart
MCAP_FILE = "mcap_data.json"
TOP_COINS_CACHE = {}
TOP_COINS_LAST_FETCH = 0
PAGES_LOADED = set()


def save_mcap_data():
    try:
        with open(MCAP_FILE, "w") as f:
            json.dump({
                "cache": TOP_COINS_CACHE,
                "last_fetch": TOP_COINS_LAST_FETCH,
                "pages": list(PAGES_LOADED)
            }, f)
    except Exception:
        pass


def load_mcap_data():
    global TOP_COINS_CACHE, TOP_COINS_LAST_FETCH, PAGES_LOADED
    try:
        import os
        if os.path.exists(MCAP_FILE):
            with open(MCAP_FILE, "r") as f:
                data = json.load(f)
                TOP_COINS_CACHE = data.get("cache", {})
                TOP_COINS_LAST_FETCH = data.get("last_fetch", 0)
                PAGES_LOADED = set(data.get("pages", []))
    except Exception:
        pass


load_mcap_data()  # Tải dữ liệu cũ ngay khi khởi động bot


async def fetch_top_coins(force=False):
    global TOP_COINS_CACHE, TOP_COINS_LAST_FETCH, PAGES_LOADED
    now = time.time()

    if force:
        PAGES_LOADED.clear()
        TOP_COINS_CACHE = {}
        save_mcap_data()

    if not force and TOP_COINS_CACHE and (now - TOP_COINS_LAST_FETCH < 3600) and len(PAGES_LOADED) >= 8:
        return len(TOP_COINS_CACHE)

    count_before = len(TOP_COINS_CACHE)

    # 1. Tải từ CoinGecko (8 trang, mỗi trang 250)
    import asyncio
    for page in range(1, 9):
        if page in PAGES_LOADED:
            continue

        retries = 3
        while retries > 0:
            try:
                url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                res = urllib.request.urlopen(req, timeout=15).read()
                data = json.loads(res)
                if not data: break
                for coin in data:
                    sym = coin['symbol'].upper()
                    if sym not in TOP_COINS_CACHE:
                        TOP_COINS_CACHE[sym] = coin['market_cap']
                PAGES_LOADED.add(page)
                save_mcap_data()
                await asyncio.sleep(2.5)
                break  # Thành công, thoát vòng lặp retry
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    print(f"Bị chặn 429 ở trang {page}. Đang đợi 30 giây để thử lại (còn {retries - 1} lần)...")
                    await asyncio.sleep(30)
                    retries -= 1
                    continue
                else:
                    print(f"Lỗi HTTP {e.code} ở trang {page}")
                    break
            except Exception as e:
                print(f"Lỗi tải CoinGecko trang {page}: {e}")
                break

        if retries == 0: break  # Dừng nếu hết lượt thử lại cho trang này

    # 2. Nếu vẫn thiếu nhiều, thử dùng CoinCap làm dự phòng
    if len(TOP_COINS_CACHE) < 1500:
        try:
            url = "https://api.coincap.io/v2/assets?limit=2000"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            if data.get('data'):
                for coin in data['data']:
                    sym = coin['symbol'].upper()
                    if sym not in TOP_COINS_CACHE and coin.get('marketCapUsd'):
                        TOP_COINS_CACHE[sym] = float(coin['marketCapUsd'])
                save_mcap_data()
        except Exception:
            pass

    # 3. Thêm CoinLore nếu vẫn chưa đủ (Quét thêm 500 đồng nữa)
    if len(TOP_COINS_CACHE) < 1500:
        for start in [0, 100, 200, 300, 400]:
            try:
                url = f"https://api.coinlore.net/api/tickers/?start={start}&limit=100"
                data = json.loads(urllib.request.urlopen(urllib.request.Request(url), timeout=10).read())
                if data.get('data'):
                    for coin in data['data']:
                        sym = coin['symbol'].upper()
                        if sym not in TOP_COINS_CACHE:
                            TOP_COINS_CACHE[sym] = float(coin['market_cap_usd'])
                save_mcap_data()
                await asyncio.sleep(1)
            except Exception:
                break

    if len(TOP_COINS_CACHE) > count_before:
        TOP_COINS_LAST_FETCH = now

    return len(TOP_COINS_CACHE)


async def get_market_caps(symbols):
    await fetch_top_coins()
    results = {s.upper(): {'mcap': None, 'exchanges': []} for s in symbols}

    # 1. Lấy vốn hóa từ Cache CoinGecko
    for s in symbols:
        s_up = s.upper()
        if s_up in TOP_COINS_CACHE:
            results[s_up]['mcap'] = TOP_COINS_CACHE[s_up]

    # 2. Bổ sung thông tin sàn giao dịch từ DexScreener cho TẤT CẢ các mã
    # Việc này giúp biết coin đó có trên Uniswap, Pancake, Raydium... không
    for sym in symbols:
        s_up = sym.upper()
        try:
            url = f"https://api.dexscreener.com/latest/dex/search?q={s_up}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if data.get('pairs'):
                # Lấy vốn hóa nếu chưa có (Chọn cặp có thanh khoản cao nhất)
                if not results[s_up]['mcap']:
                    valid_pairs = [p for p in data['pairs'] if p.get('liquidity', {}).get('usd', 0) > 0]
                    if valid_pairs:
                        best_pair = max(valid_pairs, key=lambda p: p['liquidity']['usd'])
                        curr_price = float(best_pair['priceUsd'])
                    else:
                        best_pair = data['pairs'][0]
                        curr_price = float(best_pair['priceUsd'])

                    mcap = best_pair.get('marketCap') or best_pair.get('fdv')
                    if mcap:
                        results[s_up]['mcap'] = float(mcap)

                # Lấy danh sách các sàn DEX (tối đa 3 sàn tiêu biểu)
                dexes = []
                for p in data['pairs']:
                    d_name = p['dexId'].capitalize()
                    if d_name not in dexes:
                        dexes.append(d_name)
                    if len(dexes) >= 3:
                        break
                results[s_up]['exchanges'].extend(dexes)
            time.sleep(0.2)  # DexScreener cho phép gọi nhanh
        except Exception:
            pass

    return results


def format_mcap(mcap):
    if not mcap:
        return "N/A"
    if mcap >= 1e9:
        return f"${mcap / 1e9:.2f}B"
    elif mcap >= 1e6:
        return f"${mcap / 1e6:.2f}M"
    elif mcap >= 1e3:
        return f"${mcap / 1e3:.2f}K"
    return f"${mcap:.2f}"


async def get_crypto_calls(days=7, max_pump_pct=None, pump_days=30):
    try:
        all_matches = []
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(days=days)
        time_pump_ago = (now - timedelta(days=pump_days)).isoformat()

        base_url = "https://t.me/s/thebull_crypto"
        current_url = base_url

        # Vòng lặp lấy thêm tin nhắn cũ (tối đa 15 trang để bao phủ đủ 1 tháng hoặc hơn)
        for _ in range(15):
            try:
                req = urllib.request.Request(current_url, headers={'User-Agent': 'Mozilla/5.0'})
                response = urllib.request.urlopen(req).read().decode('utf-8')

                page_matches = re.findall(
                    r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>.*?<time datetime="([^"]+)"', response,
                    re.DOTALL | re.IGNORECASE)

                if not page_matches:
                    break

                all_matches.extend(page_matches)

                # Lấy ID tin nhắn nhỏ nhất để lùi về trang trước
                msg_ids = re.findall(r'data-post="thebull_crypto/(\d+)"', response)
                if not msg_ids:
                    break

                min_id = min(int(mid) for mid in msg_ids)
                current_url = f"{base_url}?before={min_id}"

                # Kiểm tra xem tin nhắn cũ nhất trên trang này đã vượt quá threshold chưa
                oldest_time_str = page_matches[0][1]
                if datetime.fromisoformat(oldest_time_str) < threshold:
                    break

                time.sleep(0.3)
            except Exception:
                break

        # Lọc lấy tin nhắn call coin mới nhất của mỗi đồng coin
        latest_calls = {}
        for msg, time_str in all_matches:
            text = re.sub(r'<[^>]+>', ' ', msg)
            text = html.unescape(text)
            # Lọc tất cả các tag có dạng $COIN (ví dụ $HEMI, $TURTLE, $OP)
            coins = re.findall(r'\$([A-Za-z][A-Za-z0-9]{0,20})', text)
            dt = datetime.fromisoformat(time_str)
            if dt < threshold:
                continue
            for coin_raw in coins:
                coin = coin_raw.upper()
                if coin not in latest_calls or dt > datetime.fromisoformat(latest_calls[coin]):
                    latest_calls[coin] = time_str

        # Sắp xếp theo thời gian mới nhất lên đầu
        sorted_unique_calls = sorted(latest_calls.items(), key=lambda x: x[1], reverse=True)

        results = []
        for coin, time_str in sorted_unique_calls:
            # 1. Kiểm tra lọc coin đã tăng (pump) nếu có thiết lập
            hist_price, curr_price = get_crypto_prices(coin, time_str)

            if max_pump_pct is not None and curr_price is not None:
                price_pump_ago, _ = get_crypto_prices(coin, time_pump_ago)
                if price_pump_ago:
                    pump = ((curr_price - price_pump_ago) / price_pump_ago) * 100
                    if pump > max_pump_pct:
                        continue  # Bỏ qua coin đã tăng quá mức cho phép

            results.append({
                'coin': coin,
                'call_price': hist_price,
                'current_price': curr_price
            })
            if len(results) >= 100:  # Tăng giới hạn lên 100 coin để lọc cho đủ
                break

        coin_info = await get_market_caps([r['coin'] for r in results])
        for r in results:
            info = coin_info.get(r['coin'], {'mcap': None, 'exchanges': []})
            r['mcap'] = info['mcap']
            # Bổ sung Binance vào danh sách sàn nếu có giá
            exchanges = info['exchanges']
            if r['current_price'] is not None and "Binance" not in exchanges:
                exchanges.insert(0, "Binance")
            r['exchanges'] = exchanges

        return results
    except Exception as e:
        print(f"Error scraping telegram: {e}")
        return []


async def capture_arkham_flow(coin_slug):
    user_data_dir = os.path.join(os.getcwd(), "playwright_chrome_data")

    async with async_playwright() as p:
        # browser = await p.chromium.launch_persistent_context(
        #     user_data_dir=user_data_dir,
        #     # headless=False,
        #     headless=True,
        #     args=["--disable-blink-features=AutomationControlled"]
        # )
        # page = await browser.new_page()

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        page = await browser.new_page()

        # Đặt kích thước màn hình lớn để thấy rõ biểu đồ
        await page.set_viewport_size({"width": 1366, "height": 1000})

        try:
            url = f"https://intel.arkm.com/explorer/token/{coin_slug}"
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)

            # Đợi 15s để vượt qua Cloudflare và load trang
            await page.wait_for_timeout(15000)

            # 1. Click vào tab "ON-CHAIN EXCHANGE FLOW"
            try:
                # Sử dụng locator cụ thể hơn và force click để chắc chắn tab được nhấn
                tab = page.get_by_text("ON-CHAIN EXCHANGE FLOW").first
                await tab.click(force=True, timeout=5000)
                await page.wait_for_timeout(3000)  # Đợi tab kích hoạt
            except:
                pass

            # 2. Click vào nút mốc thời gian "ALL"
            try:
                # Tìm tất cả các chữ "ALL" và click (ưu tiên nút ở vùng biểu đồ)
                all_btns = await page.get_by_text("ALL", exact=True).all()
                for btn in all_btns:
                    try:
                        await btn.click(force=True, timeout=1000)
                    except:
                        pass
                await page.wait_for_timeout(4000)  # Đợi biểu đồ cập nhật dữ liệu
            except:
                pass

            screenshot_path = f"{coin_slug}_arkham.png"

            # 3. Chụp ảnh vùng biểu đồ (Exchange Flow)
            try:
                # Sử dụng JavaScript để tìm chính xác tọa độ của khung biểu đồ
                clip = await page.evaluate("""
                    () => {
                        // Ẩn cookie banner trước
                        document.querySelectorAll('[class*="Cookie"], [id*="cookie"], .cookie-banner').forEach(el => el.remove());

                        // Tìm tab ON-CHAIN EXCHANGE FLOW
                        const tabs = Array.from(document.querySelectorAll('*')).filter(el => el.innerText === 'ON-CHAIN EXCHANGE FLOW');
                        if (tabs.length === 0) return null;

                        // Tìm container cha chứa cả tab và canvas biểu đồ
                        let card = tabs[0].parentElement;
                        let limit = 0;
                        while (card && limit < 10 && (!card.querySelector('canvas') || card.offsetHeight < 250)) {
                            card = card.parentElement;
                            limit++;
                        }

                        if (!card) return null;

                        // Lấy tọa độ
                        const rect = card.getBoundingClientRect();
                        return {
                            x: rect.x,
                            y: rect.y,
                            width: rect.width,
                            height: rect.height
                        };
                    }
                """)

                if clip:
                    # Cuộn tới tọa độ đó và chụp ảnh theo khung (clip)
                    await page.mouse.wheel(0, clip['y'] - 100)
                    await page.wait_for_timeout(1000)
                    # Cập nhật lại clip sau khi cuộn
                    await page.screenshot(path=screenshot_path, clip=clip)
                else:
                    await page.screenshot(path=screenshot_path)
            except Exception as e:
                print(f"Lỗi clip: {e}")
                await page.screenshot(path=screenshot_path)

            # 4. Trích xuất dữ liệu Top Holders
            top_holders = []
            try:
                # Cuộn xuống phần Top Holders để nó được render trên trang
                try:
                    top_holder_heading = page.get_by_text("TOP HOLDERS").first
                    await top_holder_heading.scroll_into_view_if_needed()
                    await page.wait_for_timeout(3000)
                except:
                    await page.mouse.wheel(0, 3000)
                    await page.wait_for_timeout(2000)

                # Click vào nút "GROUP BY ENTITY" để gom ví theo entity
                try:
                    group_btn = page.get_by_text("GROUP BY ENTITY", exact=False).first
                    await group_btn.click(force=True, timeout=3000)
                    await page.wait_for_timeout(3000)
                except:
                    pass

                all_top_holders = []
                seen_holders = set()

                # Vòng lặp lấy tối đa 10 trang Top Holders
                for page_idx in range(10):
                    # Dùng raw text thay vì DOM selectors để tránh lấy nhầm bảng Entity Changes
                    top_holders = await page.evaluate(r"""
                        () => {
                            const NL = String.fromCharCode(10);
                            const body = document.body.innerText;
                            const lines = body.split(NL);

                            // 1. Tìm điểm bắt đầu của phần TOP HOLDERS
                            let startIdx = -1;
                            for (let i = 0; i < lines.length; i++) {
                                if (lines[i].toUpperCase().includes('TOP HOLDERS')) {
                                    startIdx = i;
                                }
                            }
                            if (startIdx === -1) return [];

                            // 2. Tìm dòng header để bắt đầu lấy dữ liệu
                            let dataIdx = -1;
                            for (let i = startIdx; i < Math.min(startIdx + 40, lines.length); i++) {
                                const txt = lines[i].toUpperCase();
                                if (txt.includes('USD') || txt.includes('PCT')) {
                                    dataIdx = i + 1;
                                    break;
                                }
                            }
                            if (dataIdx === -1) dataIdx = startIdx + 5;

                            // 3. Thu thập các dòng dữ liệu tiềm năng
                            const rawLines = [];
                            for (let i = dataIdx; i < lines.length; i++) {
                                const line = lines[i].trim();
                                if (!line) continue;
                                const up = line.toUpperCase();

                                // Dừng nếu sang section khác
                                if (up.includes('ENTITY CHANGES') || up.includes('PRICE HISTORY') || up.includes('TRANSACTIONS') || up.includes('TRANSFERS')) break;

                                // Bỏ qua header lặp lại hoặc nút bấm
                                if (['VALUE', 'PCT', 'USD', 'ENTITIES', 'ADDRESSES', 'BASE', 'COINS'].includes(up)) continue;
                                if (line.includes('GROUP BY') || (line.includes('/') && line.length < 10)) continue;

                                rawLines.push(line);
                                if (rawLines.length > 400) break;
                            }

                            // 4. Gom nhóm các dòng thành hàng (Entity - Balance - PCT - USD)
                            const rows = [];
                            let current = [];

                            for (let i = 0; i < rawLines.length; i++) {
                                const line = rawLines[i];
                                current.push(line);

                                // Một hàng kết thúc khi dòng hiện tại là giá trị USD (có $ hoặc đi sau một dòng có %)
                                const isUSD = line.includes('$');
                                const isAfterPct = i > 0 && rawLines[i-1].includes('%');

                                if (isUSD || (isAfterPct && current.length >= 3)) {
                                    rows.push([...current]);
                                    current = [];
                                    if (rows.length >= 50) break;
                                }
                            }
                            return rows;
                        }
                    """)

                    if not top_holders:
                        break

                    new_rows_added = 0
                    stop_pagination = False
                    for row in top_holders:
                        # Parse USD để kiểm tra xem có nên dừng thu thập sớm không (lọc < 1000 USD)
                        if len(row) >= 4:
                            usd_str = row[-1]
                            usd_val = 0.0
                            usd_clean = usd_str.replace('$', '').replace('<', '').replace('>', '').replace(',',
                                                                                                           '').strip()
                            if usd_clean:
                                mult = 1.0
                                if usd_clean.endswith('K'):
                                    mult = 1000.0
                                    usd_clean = usd_clean[:-1]
                                elif usd_clean.endswith('M'):
                                    mult = 1000000.0
                                    usd_clean = usd_clean[:-1]
                                elif usd_clean.endswith('B'):
                                    mult = 1000000000.0
                                    usd_clean = usd_clean[:-1]
                                try:
                                    usd_val = float(usd_clean) * mult
                                except:
                                    pass

                            if usd_val > 0 and usd_val < 1000.0:
                                stop_pagination = True
                                break  # Gặp ví < 1000 USD thì dừng ngay, không thêm vào danh sách nữa

                        row_tuple = tuple(row)
                        if row_tuple not in seen_holders:
                            seen_holders.add(row_tuple)
                            all_top_holders.append(row)
                            new_rows_added += 1

                    if stop_pagination or new_rows_added == 0:
                        break

                    # Chuyển trang bằng Javascript DOM Traversal siêu chuẩn (vì Arkham có thể bọc text trong nhiều thẻ span nhỏ)
                    has_next = False
                    try:
                        page_info = await page.evaluate(r"""
                            () => {
                                let containers = Array.from(document.querySelectorAll('[class*="paginationContainer"]'));

                                // Nếu không tìm thấy class paginationContainer, dùng SVG để định vị container
                                if (containers.length === 0) {
                                    const svgs = document.querySelectorAll('svg');
                                    for (let svg of svgs) {
                                        const cls = svg.getAttribute('class') || '';
                                        if (cls.toLowerCase().includes('chevron') || cls.toLowerCase().includes('arrow')) {
                                            if (svg.parentElement && !containers.includes(svg.parentElement)) {
                                                containers.push(svg.parentElement);
                                            }
                                        }
                                    }
                                }

                                let bestMatch = null;
                                for (let el of containers) {
                                    const rect = el.getBoundingClientRect();
                                    // Đảm bảo là nằm bên trái màn hình (bảng Top Holders thường chiếm 50-75% màn hình)
                                    if (rect.width > 0 && rect.x < window.innerWidth * 0.75) {
                                        bestMatch = el;
                                        break;
                                    }
                                }
                                if (!bestMatch && containers.length > 0) bestMatch = containers[0];

                                if (bestMatch) {
                                    let current = 0;
                                    let total = 0;

                                    // 1. Lấy Current Page từ thẻ input
                                    let input = bestMatch.querySelector('input');
                                    if (input) {
                                        current = parseInt(input.value);
                                    }

                                    // 2. Lấy Total Page từ text
                                    const txt = bestMatch.innerText || "";
                                    const fullMatch = txt.match(/(\d+)\s*\/\s*(\d+)/);
                                    const partialMatch = txt.match(/\/\s*(\d+)/);

                                    if (fullMatch) {
                                        if (!current) current = parseInt(fullMatch[1]);
                                        total = parseInt(fullMatch[2]);
                                    } else if (partialMatch) {
                                        total = parseInt(partialMatch[1]);
                                    }

                                    if (!current) current = 1;
                                    if (!total) total = 10;

                                    // 3. Tính toán vị trí click
                                    let clickRect = null;
                                    if (input) {
                                        const r = input.getBoundingClientRect();
                                        clickRect = {x: r.x, y: r.y, width: r.width, height: r.height};
                                    } else {
                                        // Nếu không có input thực sự (mà chỉ là div/span giả), click vào 1/3 bên trái của container
                                        const r = bestMatch.getBoundingClientRect();
                                        clickRect = {x: r.x, y: r.y, width: r.width * 0.3, height: r.height};
                                    }

                                    return {
                                        found: true,
                                        current: current,
                                        total: total,
                                        clickRect: clickRect
                                    };
                                }
                                return { found: false };
                            }
                        """)

                        if page_info and page_info.get("found"):
                            curr = page_info["current"]
                            total = page_info["total"]
                            print(f"===> TÌM THẤY TRANG {curr}/{total}")

                            if curr < total:
                                if page_info.get("clickRect"):
                                    txt_box = page_info["clickRect"]
                                    next_page = curr + 1
                                    print(f"===> GÕ SỐ TRANG {next_page} TẠI TỌA ĐỘ X:{txt_box['x']}, Y:{txt_box['y']}")

                                    # Click vào chính giữa input
                                    target_x = txt_box["x"] + (txt_box["width"] / 2)
                                    target_y = txt_box["y"] + (txt_box["height"] / 2)

                                    # Triple click để bôi đen toàn bộ số trang cũ (phòng khi số có 2 chữ số)
                                    await page.mouse.click(target_x, target_y, click_count=3)
                                    await page.wait_for_timeout(300)

                                    # Gõ số trang mới và Enter
                                    await page.keyboard.press("Backspace")
                                    await page.keyboard.type(str(next_page))
                                    await page.wait_for_timeout(100)
                                    await page.keyboard.press("Enter")
                                else:
                                    print("===> KHÔNG TÌM THẤY TỌA ĐỘ INPUT!")

                                has_next = True
                    except Exception as e:
                        print(f"Lỗi khi tìm và click chuyển trang: {e}")

                    if has_next:
                        await page.wait_for_timeout(6000)  # Đợi 6s cho chắc chắn trang render xong
                    else:
                        break

            except Exception as e:
                print(f"Lỗi trích xuất Top Holders: {e}")

            return {"screenshot": screenshot_path, "top_holders": all_top_holders}
        except Exception as e:
            print(f"Lỗi khi chụp ảnh Arkham: {e}")
            return {"screenshot": None, "top_holders": []}
        finally:
            await browser.close()


async def process_mm_portfolio(entity_slug, mm_name, update, context):
    try:
        user_data_dir = os.path.join(os.getcwd(), "playwright_chrome_data")
        async with async_playwright() as p:
            # browser = await p.chromium.launch_persistent_context(
            #     user_data_dir=user_data_dir,
            #     headless=False,
            #     args=["--disable-blink-features=AutomationControlled"]
            # )
            # page = await browser.new_page()

            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.set_viewport_size({"width": 1366, "height": 1000})

            try:
                url = f"https://intel.arkm.com/explorer/entity/{entity_slug}"
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(15000)

                # Cuộn để tải thêm portfolio nếu cần (mỗi lần scroll một chút)
                for _ in range(3):
                    await page.mouse.wheel(0, 1000)
                    await page.wait_for_timeout(2000)

                portfolio_lines = await page.evaluate(r"""
                    () => {
                        const NL = String.fromCharCode(10);
                        const body = document.body.innerText;
                        return body.split(NL).filter(x => x.trim().length > 0).slice(0, 1500);
                    }
                """)

                assets = []
                import re
                pattern = re.compile(r'^[\d\.,]+[KMBkmb]?\s+([A-Z0-9\-\.]+)$')

                for i in range(len(portfolio_lines) - 1):
                    prev_line = portfolio_lines[i].strip()
                    curr_line = portfolio_lines[i + 1].strip()

                    match = pattern.match(prev_line)
                    if match and curr_line.startswith('$'):
                        asset_name = match.group(1)
                        value_str = curr_line.replace('$', '').replace(',', '')

                        mult = 1.0
                        if value_str.endswith('K'):
                            mult = 1000.0
                            value_str = value_str[:-1]
                        elif value_str.endswith('M'):
                            mult = 1000000.0
                            value_str = value_str[:-1]
                        elif value_str.endswith('B'):
                            mult = 1000000000.0
                            value_str = value_str[:-1]

                        try:
                            value = float(value_str) * mult
                            if value >= 50000:
                                assets.append((asset_name, curr_line, value))
                        except Exception:
                            pass

                if not assets:
                    await update.callback_query.message.edit_text(
                        f"❌ Không tìm thấy danh mục hoặc không có token nào >= $50,000 cho {mm_name}.")
                    return

                # Xóa token trùng lặp (lấy value cao nhất nếu trùng)
                unique_assets = {}
                for a_name, a_str, a_val in assets:
                    if a_name not in unique_assets or a_val > unique_assets[a_name][1]:
                        unique_assets[a_name] = (a_str, a_val)

                sorted_assets = sorted(unique_assets.items(), key=lambda x: x[1][1], reverse=True)

                # Format tin nhắn
                msg = f"💼 *Portfolio của {mm_name}* (>= $50K)\n🔗 {url}\n\n"
                for name, (val_str, _) in sorted_assets:
                    msg += f"• `{name}`: {val_str}\n"

                # Nếu tin nhắn quá dài thì chia nhỏ
                if len(msg) > 4000:
                    lines = msg.split('\n')
                    current_chunk = ""
                    for line in lines:
                        if len(current_chunk) + len(line) + 1 > 4000:
                            await update.callback_query.message.reply_text(current_chunk, parse_mode='Markdown')
                            current_chunk = line + "\n"
                        else:
                            current_chunk += line + "\n"
                    if current_chunk.strip():
                        await update.callback_query.message.reply_text(current_chunk, parse_mode='Markdown')
                    await update.callback_query.message.delete()
                else:
                    await update.callback_query.message.edit_text(msg, parse_mode='Markdown')

            except Exception as e:
                await update.callback_query.message.edit_text(f"❌ Lỗi khi tải dữ liệu {mm_name}: {e}")
            finally:
                await browser.close()
    except Exception as e:
        await update.callback_query.message.edit_text(f"❌ Lỗi khởi tạo trình duyệt: {e}")


async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_obj = update.message if update.message else update.callback_query.message
    await msg_obj.reply_text(f'Hello {update.effective_user.first_name}')


async def call_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = context.user_data.get('lookback_days', 7)
    min_mcap = context.user_data.get('min_mcap', 10 * 1e6)
    max_pump = context.user_data.get('max_pump_pct')
    pump_days = context.user_data.get('pump_lookback_days', 30)
    calls = await get_crypto_calls(days=days, max_pump_pct=max_pump, pump_days=pump_days)

    # Lọc theo vốn hóa tối thiểu
    if min_mcap:
        calls = [c for c in calls if c.get('mcap') and c['mcap'] >= min_mcap]

    time_label = f"{days} ngày"
    mcap_label = f"{min_mcap / 1e6:g}M"
    pump_label = f" | Pump < {max_pump}% ({pump_days}d)" if max_pump else ""

    if calls:
        message = f"📌 *THÔNG TIN CALL ({time_label} | Vốn hóa >= {mcap_label}{pump_label})*\n\n"
        for i, call in enumerate(calls, 1):
            coin = call['coin']
            call_price = call['call_price']
            curr_price = call['current_price']
            mcap_val = call.get('mcap')

            mcap_str = f"\n  • Vốn hóa: {format_mcap(mcap_val)}"

            ex_str = f"\n  • Sàn: {', '.join(call.get('exchanges', []))}" if call.get('exchanges') else ""

            price_str = ""
            if curr_price is None:
                price_str = f"\n  • Giá: Chưa list trên Binance{mcap_str}{ex_str}"
            elif call_price is not None:
                change = ((curr_price - call_price) / call_price) * 100
                trend = "📈" if change >= 0 else "📉"
                price_str = f"\n  • Giá lúc call: {call_price:g}$\n  • Giá hiện tại: {curr_price:g}$ ({trend} {change:.2f}%){mcap_str}{ex_str}"
            else:
                price_str = f"\n  • Giá lúc call: N/A\n  • Giá hiện tại: {curr_price:g}${mcap_str}{ex_str}"

            message += f"{i}. ${coin}{price_str}\n\n"
    else:
        message = f"Không tìm thấy coin nào được call trong {time_label} qua."

    msg_obj = update.message if update.message else update.callback_query.message
    await msg_obj.reply_text(message, parse_mode='Markdown')


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = context.user_data.get('lookback_days', 7)
    min_mcap = context.user_data.get('min_mcap', 10 * 1e6)
    max_pump = context.user_data.get('max_pump_pct')
    pump_days = context.user_data.get('pump_lookback_days', 30)
    calls = await get_crypto_calls(days=days, max_pump_pct=max_pump, pump_days=pump_days)

    # Chỉ lấy những coin có đủ giá call và giá hiện tại
    valid_calls = [c for c in calls if c['call_price'] is not None and c['current_price'] is not None]

    if min_mcap is not None:
        valid_calls = [c for c in valid_calls if c.get('mcap') and c['mcap'] >= min_mcap]

    msg_obj = update.message if update.message else update.callback_query.message
    if not valid_calls:
        await msg_obj.reply_text(f"Không có đủ dữ liệu giá để phân tích trong {days} ngày qua.")
        return

    up_count = 0
    down_count = 0
    up_coins = []
    down_coins = []
    total_initial = len(valid_calls)
    total_final = 0

    up_total_pct = 0
    down_total_pct = 0
    for call in valid_calls:
        change = (call['current_price'] - call['call_price']) / call['call_price']
        change_pct = change * 100
        if change >= 0:
            up_count += 1
            up_coins.append(f"${call['coin']} (+{change_pct:.1f}%)")
            up_total_pct += change_pct
        else:
            down_count += 1
            down_coins.append(f"${call['coin']} ({change_pct:.1f}%)")
            down_total_pct += change_pct

        # Tính tỷ lệ giá trị còn lại nếu đầu tư 1$ vào đồng này
        final_value_for_one_dollar = call['current_price'] / call['call_price']
        total_final += final_value_for_one_dollar

    total_pct_change = up_total_pct + down_total_pct
    total_trend = "📈 LÃI" if total_pct_change >= 0 else "📉 LỖ"
    overall_change = ((total_final - total_initial) / total_initial) * 100
    trend = "📈 LÃI" if overall_change >= 0 else "📉 LỖ"

    mcap_label = f"{min_mcap / 1e6:g}M"
    pump_label = f" | Pump < {max_pump}% ({pump_days}d)" if max_pump else ""
    message = (
        f"📊 *PHÂN TÍCH THE BULL CRYPTO ({days} Ngày | Vốn hóa >= {mcap_label}{pump_label})*\n\n"
        f"Tổng số lệnh phân tích: {len(valid_calls)}\n"
        f"✅ Số coin tăng: {up_count}\n"
        f"{', '.join(up_coins)}\n"
        f"➕ Tổng % tăng các coin: +{up_total_pct:.1f}%\n\n"
        f"❌ Số coin giảm: {down_count}\n"
        f"{', '.join(down_coins)}\n"
        f"➖ Tổng % giảm các coin: {down_total_pct:.1f}%\n\n"
        f"Tỷ lệ thắng (Win Rate): {(up_count / len(valid_calls)) * 100:.1f}%\n\n"
        f"💰 *Hiệu suất danh mục đầu tư:*\n"
        f"_(Giả định vào tất cả các coin với cùng một số tiền)_\n"
        f"Tổng biến động (Cộng dồn): {total_trend} *{total_pct_change:.2f}%*\n"
        f"Hiệu suất thực tế (Bình quân): {trend} *{overall_change:.2f}%*"
    )

    await msg_obj.reply_text(message, parse_mode='Markdown')


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = context.user_data.get('lookback_days', 7)
    min_mcap = context.user_data.get('min_mcap', 10 * 1e6)
    max_pump = context.user_data.get('max_pump_pct')
    pump_days = context.user_data.get('pump_lookback_days', 30)

    mcap_label = f"{min_mcap / 1e6:g}M"
    pump_label = f"{max_pump}%" if max_pump is not None else "Không lọc"

    keyboard = [
        [InlineKeyboardButton("📅 Số ngày xem lại", callback_data='lookback'),
         InlineKeyboardButton("🔍 Lọc vốn hóa", callback_data='filter_mcap')],
        [InlineKeyboardButton("🚫 Chặn coin tăng", callback_data='filter_pump'),
         InlineKeyboardButton("⏳ Ngày check pump", callback_data='pump_lookback')],
        [InlineKeyboardButton("🏦 Tra cứu MM", callback_data='lookup_mm'),
         InlineKeyboardButton("🔍 Tra cứu Arkham", callback_data='check_arkham')],
        [InlineKeyboardButton("🔥 Lấy thông tin Call", callback_data='call')],
        [InlineKeyboardButton("📊 Phân tích hiệu suất", callback_data='analyze')],
        [InlineKeyboardButton("💰 Cập nhật MarketCap", callback_data='marketcap')],
        [InlineKeyboardButton("🧹 Dọn dẹp chat", callback_data='clear')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_obj = update.message if update.message else update.callback_query.message

    text = (f"👇 Thiết lập hiện tại:\n"
            f"• Xem lại: *{days} ngày*\n"
            f"• Vốn hóa tối thiểu: *{mcap_label}*\n"
            f"• Chặn coin tăng > *{pump_label}* (trong *{pump_days} ngày*)\n\n"
            f"Vui lòng chọn chức năng:")

    await msg_obj.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)


async def marketcap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_obj = update.message if update.message else update.callback_query.message
    status_msg = await msg_obj.reply_text("⏳ Đang tải dữ liệu vốn hóa từ CoinGecko, vui lòng đợi...")

    count = await fetch_top_coins(force=False)  # Tải thêm nếu chưa đủ 1 tiếng hoặc tải mới

    message = f"✅ Đã tải dữ liệu vốn hóa cho *{count}* đồng coin lớn nhất."
    keyboard = []
    if count < 1800:  # Nếu chưa đạt mốc ~2000 do lỗi/rate limit
        keyboard.append([InlineKeyboardButton("🔄 Tải thêm data", callback_data='load_more_mcap')])
        message += "\n\n⚠️ Có vẻ dữ liệu chưa được tải đầy đủ do giới hạn API."

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await status_msg.edit_text(message, parse_mode='Markdown', reply_markup=reply_markup)


async def lookback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_obj = update.message if update.message else update.callback_query.message
    await msg_obj.reply_text("Vui lòng nhập số ngày bạn muốn xem lại dữ liệu (ví dụ: 1, 7, 30):")
    context.user_data['awaiting_lookback'] = True


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == 'call':
        await call_command(update, context)
    elif query.data == 'analyze':
        await analyze_command(update, context)
    elif query.data == 'marketcap' or query.data == 'load_more_mcap':
        await marketcap_command(update, context)
    elif query.data == 'lookback':
        await lookback_command(update, context)
    elif query.data == 'filter_mcap':
        await query.message.reply_text(
            "Vui lòng nhập giá trị vốn hóa tối thiểu (đơn vị Triệu USD - ví dụ nhập 50 để lọc coin >= $50M):")
        context.user_data['awaiting_mcap'] = True
    elif query.data == 'filter_pump':
        await query.message.reply_text("Vui lòng nhập % tăng tối đa (ví dụ nhập 10 để bỏ qua các coin đã tăng > 10%):")
        context.user_data['awaiting_pump'] = True
    elif query.data == 'pump_lookback':
        await query.message.reply_text("Vui lòng nhập số ngày để kiểm tra pump (mặc định là 30):")
        context.user_data['awaiting_pump_lookback'] = True
    elif query.data == 'check_arkham':
        await query.message.reply_text("Vui lòng nhập tên/slug của coin trên Arkham:")
        context.user_data['awaiting_arkham'] = True
    elif query.data == 'lookup_mm':
        keyboard = [
            [InlineKeyboardButton("Wintermute", callback_data='mm_wintermute')],
            [InlineKeyboardButton("DWF Labs", callback_data='mm_dwflabs')],
            [InlineKeyboardButton("GSR Markets", callback_data='mm_gsrmarkets')],
            [InlineKeyboardButton("World Liberty Fi", callback_data='mm_worldlibertyfi')],
            [InlineKeyboardButton("Cumberland DRW", callback_data='mm_cumberland')],
            [InlineKeyboardButton("« Quay lại Menu", callback_data='back_to_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text("Vui lòng chọn Market Maker cần tra cứu:", reply_markup=reply_markup)
    elif query.data == 'back_to_menu':
        await menu_command(update, context)
        try:
            await query.message.delete()
        except:
            pass
    elif query.data == 'mm_wintermute':
        await query.message.edit_text("⏳ Đang tra cứu danh mục của Wintermute trên Arkham (chỉ lấy >= $50K)...")
        await process_mm_portfolio("wintermute", "Wintermute", update, context)
    elif query.data == 'mm_dwflabs':
        await query.message.edit_text("⏳ Đang tra cứu danh mục của DWF Labs trên Arkham (chỉ lấy >= $50K)...")
        await process_mm_portfolio("dwf-labs", "DWF Labs", update, context)
    elif query.data == 'mm_gsrmarkets':
        await query.message.edit_text("⏳ Đang tra cứu danh mục của GSR Markets trên Arkham (chỉ lấy >= $50K)...")
        await process_mm_portfolio("gsr-markets", "GSR Markets", update, context)
    elif query.data == 'mm_worldlibertyfi':
        await query.message.edit_text("⏳ Đang tra cứu danh mục của World Liberty Fi trên Arkham (chỉ lấy >= $50K)...")
        await process_mm_portfolio("worldlibertyfi", "World Liberty Fi", update, context)
    elif query.data == 'mm_cumberland':
        await query.message.edit_text("⏳ Đang tra cứu danh mục của Cumberland DRW trên Arkham (chỉ lấy >= $50K)...")
        await process_mm_portfolio("cumberland", "Cumberland DRW", update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get('awaiting_mcap'):
        try:
            val = float(update.message.text.strip())
            context.user_data['min_mcap'] = val * 1e6
            context.user_data['awaiting_mcap'] = False
            await menu_command(update, context)
        except ValueError:
            await update.message.reply_text("Vui lòng nhập một con số hợp lệ (ví dụ: 50).")
    elif context.user_data.get('awaiting_pump'):
        try:
            val = float(update.message.text.strip())
            if val <= 0:
                context.user_data['max_pump_pct'] = None
            else:
                context.user_data['max_pump_pct'] = val
            context.user_data['awaiting_pump'] = False
            await menu_command(update, context)
        except ValueError:
            await update.message.reply_text("Vui lòng nhập một con số hợp lệ (ví dụ: 10).")
    elif context.user_data.get('awaiting_pump_lookback'):
        try:
            val = int(update.message.text.strip())
            context.user_data['pump_lookback_days'] = val
            context.user_data['awaiting_pump_lookback'] = False
            await menu_command(update, context)
        except ValueError:
            await update.message.reply_text("Vui lòng nhập một con số hợp lệ (ví dụ: 30).")
    elif context.user_data.get('awaiting_lookback'):
        try:
            days = int(update.message.text.strip())
            context.user_data['lookback_days'] = days
            context.user_data['awaiting_lookback'] = False
            await menu_command(update, context)
        except ValueError:
            await update.message.reply_text("Vui lòng nhập một con số hợp lệ (ví dụ: 30).")
    elif context.user_data.get('awaiting_arkham'):
        raw_input = update.message.text.strip().lower()
        context.user_data['awaiting_arkham'] = False

        status_msg = await update.message.reply_text(f"🔍 Đang tìm tên đầy đủ cho `{raw_input}`...",
                                                     parse_mode='Markdown')

        import urllib.request
        import urllib.parse
        import json

        coin_slug = raw_input.replace(" ", "-")
        try:
            url = f"https://api.coingecko.com/api/v3/search?query={urllib.parse.quote(raw_input)}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            res = urllib.request.urlopen(req, timeout=10).read()
            data = json.loads(res)
            if data.get('coins') and len(data['coins']) > 0:
                coin_slug = data['coins'][0]['id']
                await status_msg.edit_text(
                    f"✅ Đã tìm thấy coin: `{coin_slug}`\n⏳ Đang truy cập Arkham, vui lòng đợi 20-30s...",
                    parse_mode='Markdown')
            else:
                await status_msg.edit_text(
                    f"⏳ Không tìm thấy trên CoinGecko, đang thử dùng trực tiếp: `{coin_slug}`\n⏳ Đang truy cập Arkham...",
                    parse_mode='Markdown')
        except Exception as e:
            await status_msg.edit_text(f"⚠️ Lỗi tìm kiếm, dùng trực tiếp: `{coin_slug}`\n⏳ Đang truy cập Arkham...",
                                       parse_mode='Markdown')

        try:
            result = await capture_arkham_flow(coin_slug)
            photo_path = result.get("screenshot")
            top_holders = result.get("top_holders", [])

            if photo_path and os.path.exists(photo_path):
                arkham_url = f"https://intel.arkm.com/explorer/token/{coin_slug}"

                # 1. Gửi link ra một bong bóng chat riêng đầu tiên
                link_msg = f"📊 Bảng dữ liệu Arkham cho: `{coin_slug}`\n🔗 Link: {arkham_url}"
                await update.message.reply_text(link_msg, parse_mode='Markdown')

                # Định dạng dữ liệu Top Holders
                holders_text = ""
                if top_holders:
                    holders_text = "🔝 *TOP HOLDERS:*\n"
                    for row in top_holders:
                        if len(row) >= 4:
                            # Cột cuối: USD, trước đó: PCT, trước nữa: VALUE, còn lại: tên
                            usd = row[-1]
                            pct = row[-2]
                            name = " ".join(row[:-3])

                            # Parse USD để lọc ví < 1000 USD
                            usd_val = 0.0
                            usd_clean = usd.replace('$', '').replace('<', '').replace('>', '').replace(',', '').strip()
                            if usd_clean:
                                mult = 1.0
                                if usd_clean.endswith('K'):
                                    mult = 1000.0
                                    usd_clean = usd_clean[:-1]
                                elif usd_clean.endswith('M'):
                                    mult = 1000000.0
                                    usd_clean = usd_clean[:-1]
                                elif usd_clean.endswith('B'):
                                    mult = 1000000000.0
                                    usd_clean = usd_clean[:-1]
                                try:
                                    usd_val = float(usd_clean) * mult
                                except:
                                    pass

                            # Lọc bỏ ví có giá trị < 1000 USD
                            if usd_val < 1000.0:
                                continue

                            # Kiểm tra nếu PCT >= 1% thì đánh dấu
                            mark = ""
                            pct_value = 0.0
                            try:
                                pct_value = float(pct.replace('%', '').strip())
                                if pct_value >= 1.0:
                                    mark = " 🚩"
                            except:
                                pass

                            name_clean = name.replace('\xa0', '').replace('\u200b', '').strip()
                            # Lọc bỏ ví chưa định danh (0x... dài hơn 40 ký tự) nếu có pct < 1%
                            if name_clean.startswith('0x') and len(name_clean) >= 40 and pct_value < 1.0:
                                continue

                            holders_text += f"• `{name_clean}`: {pct} - {usd}{mark}\n"
                        elif len(row) >= 2:
                            holders_text += f"• " + " | ".join(row) + "\n"

                # 2. Gửi ảnh chụp màn hình
                await update.message.reply_photo(photo=open(photo_path, 'rb'))

                # 3. Gửi danh sách Top Holders
                if holders_text:
                    if len(holders_text) > 4000:
                        lines = holders_text.split('\n')
                        current_chunk = ""
                        for line in lines:
                            if len(current_chunk) + len(line) + 1 > 4000:
                                await update.message.reply_text(current_chunk, parse_mode='Markdown')
                                current_chunk = line + "\n"
                            else:
                                current_chunk += line + "\n"
                        if current_chunk.strip():
                            await update.message.reply_text(current_chunk, parse_mode='Markdown')
                    else:
                        await update.message.reply_text(holders_text, parse_mode='Markdown')

                await status_msg.delete()
                os.remove(photo_path)
            else:
                await status_msg.edit_text(
                    f"❌ Không thể chụp được ảnh trang Arkham cho `{coin_slug}`. Có thể do chặn Cloudflare hoặc lỗi mạng.")
        except Exception as e:
            await status_msg.edit_text(f"❌ Có lỗi xảy ra: {e}")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio
    chat_id = update.effective_chat.id
    msg_obj = update.message if update.message else update.callback_query.message
    current_msg_id = msg_obj.message_id

    # Send a temporary status message (don't delete this immediately)
    try:
        status_msg = await context.bot.send_message(chat_id=chat_id, text="🧹 Đang dọn dẹp tin nhắn, vui lòng đợi...")
    except Exception:
        status_msg = None

    # Try to delete the last 50 messages
    for i in range(current_msg_id, max(0, current_msg_id - 60), -1):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=i)
        except Exception:
            pass

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    # Gửi tin nhắn xác nhận và tự động xóa sau 3s
    try:
        final_msg = await context.bot.send_message(chat_id=chat_id, text="✨ Giao diện đã được dọn dẹp sạch sẽ như mới!")
        await asyncio.sleep(3)
        await final_msg.delete()
    except Exception:
        pass


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("menu", "Hiển thị menu chức năng"),
        BotCommand("lookback", "Chọn số ngày xem lại dữ liệu"),
        BotCommand("marketcap", "Cập nhật dữ liệu vốn hóa"),
        BotCommand("call", "Lấy các coin được call"),
        BotCommand("analyze", "Phân tích hiệu suất"),
        BotCommand("clear", "Dọn dẹp giao diện chat"),
    ])


# app = ApplicationBuilder().token("8297899430:AAFj5L57eegAl0QqUPz5868vPlP_D5mtoB4").post_init(post_init).build()
TOKEN = os.getenv("BOT_TOKEN")

print(f"TOKEN loaded: {'OK' if TOKEN else 'NONE - THIẾU ENV VAR!'}")

app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

app.add_handler(CommandHandler("hello", hello))
app.add_handler(CommandHandler("lookback", lookback_command))
app.add_handler(CommandHandler("marketcap", marketcap_command))
app.add_handler(CommandHandler("call", call_command))
app.add_handler(CommandHandler("analyze", analyze_command))
app.add_handler(CommandHandler("menu", menu_command))
app.add_handler(CommandHandler("start", menu_command))
app.add_handler(CommandHandler("clear", clear_command))
app.add_handler(CallbackQueryHandler(button_callback))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

from flask import Flask
from threading import Thread

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

# if __name__ == '__main__':
#     app.run_polling()

# if __name__ == '__main__':
#     Thread(target=run_web).start()
#     app.run_polling()


if __name__ == '__main__':
    import asyncio

    # Fix cho Python 3.10+ / 3.14: tạo event loop thủ công
    asyncio.set_event_loop(asyncio.new_event_loop())

    Thread(target=run_web, daemon=True).start()

    try:
        print("🤖 Đang khởi động Telegram bot...")
        app.run_polling()
    except Exception as e:
        import traceback

        print(f"❌ Bot crash: {e}")
        traceback.print_exc()