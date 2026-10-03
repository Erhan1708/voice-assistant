# Голосовой ассистент для Linux (GNOME)

Локальный голосовой ассистент, который управляет компьютером по горячей клавише.
Всё работает на вашей машине: речь распознаёт Whisper, команды понимает локальная
модель через Ollama, ответ озвучивает Piper. В интернет ничего не отправляется.

```
горячая клавиша → запись с микрофона → Whisper → Ollama (qwen3:8b) → действие → голосовой ответ
```

## Что умеет

- Запуск и закрытие программ, открытие сайтов и папок, поиск в интернете
- Громкость, пауза и переключение треков, скриншоты
- Блокировка экрана, спящий режим, выключение и перезагрузка
- Файлы: поиск, просмотр папок, создание, копирование, перемещение, удаление в корзину
- Ответы на вопросы голосом
- Короткая память диалога: «создай папку отчёты», затем «создай в ней файл…»

## Безопасность

Модель не имеет доступа к терминалу. Ей доступны только 16 заранее описанных действий.

- Выключение, перезагрузка, выход из сеанса и удаление требуют подтверждения в окне
- Удаление идёт в корзину
- Изменять файлы можно только в домашней папке; скрытые файлы и перезапись запрещены
- Неуверенно распознанные фразы отбрасываются и не выполняются

## Требования

- Linux с GNOME на Wayland (проверено на Fedora 44, GNOME 50), PipeWire
- Python 3.10+ с системным пакетом PyGObject (`python3-gobject`)
- [Ollama](https://ollama.com) с моделью, поддерживающей вызов инструментов
- Утилиты: `pw-record`, `pw-play`, `wpctl`, `notify-send`, `zenity`, `gtk-launch`, `gio`
- Видеокарта с 8 ГБ памяти для qwen3:8b. Whisper работает на процессоре

## Установка

```bash
git clone git@github.com:Erhan1708/-voice-assistant.git ~/voice-assistant
cd ~/voice-assistant

# окружение (системные пакеты нужны для PyGObject)
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt

# русский голос для синтеза речи
.venv/bin/python -m piper.download_voices ru_RU-irina-medium --download-dir voices

# модель
ollama pull qwen3:8b

# фоновая служба
mkdir -p ~/.config/systemd/user
cp voice-assistant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voice-assistant
```

Горячая клавиша Ctrl+Alt+Пробел:

```bash
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-assistant/
S=org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$P
gsettings set $S name 'Голосовой ассистент'
gsettings set $S command "$HOME/voice-assistant/.venv/bin/python $HOME/voice-assistant/assistant.py toggle"
gsettings set $S binding '<Control><Alt>space'
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$P']"
```

Если у вас уже есть свои сочетания клавиш, добавьте путь `$P` в существующий список,
а не заменяйте его.

## Использование

1. Нажмите Ctrl+Alt+Пробел, прозвучит сигнал.
2. Скажите команду. Запись остановится сама после паузы или по повторному нажатию.
3. Нажатие во время обработки или ответа отменяет его.

Примеры: «открой калькулятор», «сделай громкость сорок процентов», «следующий трек»,
«найди в документах файлы pdf», «сделай скриншот», «сколько будет 17 на 6».

Команду можно дать и текстом:

```bash
.venv/bin/python assistant.py text "открой загрузки"
.venv/bin/python assistant.py text --dry "выключи компьютер"   # показать действие, не выполняя
.venv/bin/python assistant.py status
```

Журнал работы: `journalctl --user -u voice-assistant -f`

## Настройка

Создайте `config.json` рядом со скриптом и переопределите нужные ключи. Все ключи и
значения по умолчанию перечислены в словаре `CONFIG` в начале `assistant.py`.

```json
{
  "model": "qwen3:8b",
  "whisper_model": "medium",
  "speak": true,
  "silence_sec": 1.1
}
```

| Модель Whisper | Время на фразу (Ryzen 5 5600) | Точность |
|---|---|---|
| `small` | около 1 с | базовая |
| `medium` | около 3 с | хорошая, по умолчанию |
| `large-v3-turbo` | около 4,5 с | лучшая |

После изменения настроек: `systemctl --user restart voice-assistant`

## Ограничения

- Управление мышью и клики по окнам не поддерживаются: Wayland это ограничивает,
  а модель не видит экран
- Яркость внешнего монитора работает только при установленном `ddcutil`
- Модель на 8 млрд параметров иногда ошибается в формулировке ответа
- Список приложений берётся из установленных `.desktop` файлов
