async def get_fuel_prices(show_delta=False):
    # Используем российский агрегатор benzinstatus.ru (медианные цены по Иваново)
    url = "https://benzinstatus.ru/ivanovo/ceny/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9"
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=12) as resp:
                if resp.status != 200:
                    return "⚠️ Не удалось получить данные о ценах (сайт недоступен)."
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                
                # Ищем строки таблицы с медианными ценами
                prices = {}
                rows = soup.find_all("tr")
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        fuel_name = cells[0].get_text(strip=True).upper()
                        price_text = cells[1].get_text(strip=True)
                        
                        # Извлекаем число из строки вида "66,00 ₽"
                        match = re.search(r'(\d+[.,]\d+)', price_text)
                        if match:
                            price_val = float(match.group(1).replace(',', '.'))
                            if "АИ-92" in fuel_name:
                                prices["АИ-92"] = price_val
                            elif "АИ-95" in fuel_name:
                                prices["АИ-95"] = price_val
                            elif "ДТ" in fuel_name:
                                prices["ДТ"] = price_val

                if not prices:
                    return "⚠️ Не удалось распознать цены на сайте. Возможно, изменилась верстка."

                text = "⛽ <b>Медианные цены на топливо (Иваново):</b>\n\n"
                
                if show_delta:
                    yesterday_prices = get_yesterday_fuel()
                    if yesterday_prices:
                        text += format_price_with_delta("АИ-92", prices.get("АИ-92"), yesterday_prices.get("АИ-92"))
                        text += format_price_with_delta("АИ-95", prices.get("АИ-95"), yesterday_prices.get("АИ-95"))
                        text += format_price_with_delta("ДТ", prices.get("ДТ"), yesterday_prices.get("ДТ"))
                    else:
                        text += f"🔹 <b>АИ-92:</b> {prices.get('АИ-92', '—')} ₽/л\n"
                        text += f"🔹 <b>АИ-95:</b> {prices.get('АИ-95', '—')} ₽/л\n"
                        text += f"🔹 <b>ДТ:</b> {prices.get('ДТ', '—')} ₽/л\n"
                    
                    # Сохраняем сегодняшние цены в историю для завтрашнего сравнения
                    save_today_fuel(prices.get("АИ-92"), prices.get("АИ-95"), prices.get("ДТ"))
                else:
                    if "АИ-92" in prices: text += f"🔹 <b>АИ-92:</b> {prices['АИ-92']:.2f} ₽/л\n"
                    if "АИ-95" in prices: text += f"🔹 <b>АИ-95:</b> {prices['АИ-95']:.2f} ₽/л\n"
                    if "ДТ" in prices: text += f"🔹 <b>ДТ:</b> {prices['ДТ']:.2f} ₽/л\n"

                text += "\n<i>Данные обновляются автоматически (медиана по АЗС Иваново).</i>"
                return text
        except Exception as e:
            print(f"Ошибка топлива: {e}")
            return "⚠️ Ошибка при запросе цен на топливо."
