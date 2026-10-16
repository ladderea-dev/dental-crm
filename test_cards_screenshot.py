import time
import os
import sys
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    chrome_options = Options()
    chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1440,900')
    driver = webdriver.Chrome(options=chrome_options)

    try:
        driver.get('https://voice.airzensg.com/')
        time.sleep(2)
        driver.find_element(By.ID, 'login-username').send_keys('admin')
        driver.find_element(By.ID, 'login-password').send_keys('AirzenAdmin2026!')
        driver.find_element(By.CSS_SELECTOR, "#login-form button[type='submit']").click()
        time.sleep(3)

        driver.execute_script("switchTab('telecaller');")
        time.sleep(2)

        res = driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            (async () => {
                _tcScope = 'all';
                await loadTelecallerWorkspace();
                tcSelectBucket('due_today');
                done({
                    cards: document.querySelectorAll('.tc-queue-cards .tc-card').length,
                    bucketText: document.getElementById('tc-bkt-due_today')?.textContent,
                    listLength: (_tcCurrentList || []).length,
                    renderedQueue: (window._tcRenderedQueue || []).length
                });
            })();
        """)
        print("Script result:", res)
        time.sleep(1)

        cards = driver.find_elements(By.CSS_SELECTOR, '.tc-queue-cards .tc-card')
        print(f'Found {len(cards)} queue cards in Due Today bucket.')
        for i, c in enumerate(cards[:5]):
            print(f'Card #{i+1}: {c.text.replace(chr(10), " | ")}')

        screenshot_path = r'C:\Users\Sniki\.gemini\antigravity\brain\51971fec-6fed-486e-8bd3-be49838e7764\telecaller_cards_rendered.png'
        driver.save_screenshot(screenshot_path)
        print(f'Saved live rendered cards screenshot to: {screenshot_path}')

    finally:
        driver.quit()

if __name__ == '__main__':
    main()
