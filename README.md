# SpeakSale

Speak a sale and it becomes a priced record in PostgreSQL. Built for my stationery shop.

**Pipeline:** audio -> Gemini (transcribe + structure) -> product matching -> pricing and discount -> database, with a review queue for orders that can't be matched.

**Status:** version 1 (notebook). Next: recording queue, Streamlit app, hotkey recorder.
