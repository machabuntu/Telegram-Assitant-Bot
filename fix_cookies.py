#!/usr/bin/env python3
"""
Скрипт для исправления файла cookies
"""

import os
import sys

def fix_cookies_file(cookies_file: str):
    """Исправляет кодировку файла cookies"""
    if not os.path.exists(cookies_file):
        print(f"❌ Файл {cookies_file} не найден!")
        return False
    
    print(f"🔧 Исправляю файл cookies: {cookies_file}")
    
    try:
        # Пробуем разные кодировки
        for encoding in ['utf-8', 'cp1251', 'latin1', 'iso-8859-1', 'windows-1252']:
            try:
                with open(cookies_file, 'r', encoding=encoding) as f:
                    content = f.read()
                
                # Если файл успешно прочитан, сохраняем в UTF-8
                with open(cookies_file, 'w', encoding='utf-8') as f:
                    f.write(content)
                
                print(f"✅ Cookies файл исправлен! (исходная кодировка: {encoding})")
                return True
                
            except UnicodeDecodeError:
                continue
        
        # Если все кодировки не подошли, читаем как байты
        print("⚠️  Не удалось определить кодировку, читаю как байты...")
        
        with open(cookies_file, 'rb') as f:
            content = f.read()
        
        # Декодируем с заменой нечитаемых символов
        text_content = content.decode('utf-8', errors='replace')
        
        with open(cookies_file, 'w', encoding='utf-8') as f:
            f.write(text_content)
        
        print("✅ Cookies файл исправлен с заменой нечитаемых символов")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при исправлении файла: {e}")
        return False

def main():
    """Главная функция"""
    cookies_file = "cookies.txt"
    
    if len(sys.argv) > 1:
        cookies_file = sys.argv[1]
    
    print("🍪 Исправление файла cookies для yt-dlp")
    print("=" * 50)
    
    if fix_cookies_file(cookies_file):
        print("\n🎉 Файл cookies готов к использованию!")
        print("Теперь можно запускать бота.")
    else:
        print("\n❌ Не удалось исправить файл cookies.")
        print("Попробуйте экспортировать cookies заново.")

if __name__ == "__main__":
    main()


