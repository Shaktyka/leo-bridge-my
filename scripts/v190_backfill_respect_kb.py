"""
v1.9.0 α.4: Backfill embeddings для ai.respect_kb.

Читает все карточки WHERE embedding IS NULL батчами по 200,
кодирует через app/embedder.py (multilingual-e5-large),
записывает обратно.

Идемпотентно: можно прерывать и перезапускать.
Progress через tqdm каждые 500 карточек.
"""
import asyncio
import asyncpg
import sys
import time
from pathlib import Path

# Добавляем bridge/ в PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.embedder import encode_passages, vector_literal, MODEL_TAG
from dotenv import load_dotenv
import os

# Загружаем .env
load_dotenv("/opt/ai/bridge/.env")

BATCH_SIZE = 200      # читаем из БД
ENCODE_BATCH = 32     # кодируем за раз
PROGRESS_EVERY = 500  # чекпойнт


async def main():
    print("v1.9.0 α.4: Backfill embeddings для respect_kb")
    print(f"Model: multilingual-e5-large → 1024-dim")
    print()
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # 1. Подсчёт работы
        total = await conn.fetchval(
            "SELECT count(*) FROM ai.respect_kb WHERE embedding IS NULL"
        )
        print(f"Карточек без embedding: {total}")
        
        if total == 0:
            print("✓ Все карточки уже имеют embedding")
            return
            
        # 2. Прогрев модели
        print("Прогрев модели...")
        t0 = time.time()
        await encode_passages(["test warmup passage"])
        print(f"Модель загружена за {time.time()-t0:.1f}s")
        print()
        
        # 3. Основной цикл
        processed = 0
        start_time = time.time()
        
        while True:
            # Читаем батч
            rows = await conn.fetch("""
                SELECT content_id, title, indexable_text
                FROM ai.respect_kb 
                WHERE embedding IS NULL
                ORDER BY content_id
                LIMIT $1
            """, BATCH_SIZE)
            
            if not rows:
                break
                
            print(f"Обрабатываем батч {len(rows)} карточек...")
            
            # Подготавливаем тексты для embedding
            passages = []
            for row in rows:
                # Объединяем title + indexable_text, обрезаем до 8000 символов
                title = row["title"] or ""
                text = row["indexable_text"] or ""
                passage = f"{title}\\n\\n{text}"[:8000]
                passages.append(passage)
            
            # Кодируем батчами по ENCODE_BATCH
            all_embeddings = []
            for i in range(0, len(passages), ENCODE_BATCH):
                chunk = passages[i:i + ENCODE_BATCH]
                embs = await encode_passages(chunk, batch_size=ENCODE_BATCH)
                all_embeddings.extend(embs)
            
            # Записываем обратно
            updates = [
                (vector_literal(emb), MODEL_TAG, row["content_id"]) 
                for emb, row in zip(all_embeddings, rows)
            ]
            
            await conn.executemany("""
                UPDATE ai.respect_kb 
                SET embedding = $1::vector,
                    embedding_model = $2,
                    embedding_updated_at = now()
                WHERE content_id = $3
            """, updates)
            
            processed += len(rows)
            elapsed = time.time() - start_time
            rate = processed / elapsed
            eta_seconds = (total - processed) / rate if rate > 0 else 0
            
            print(f"  ✓ {processed}/{total} ({100*processed/total:.1f}%)")
            print(f"    Скорость: {rate:.1f} карточек/сек")
            print(f"    ETA: {eta_seconds/60:.1f} минут")
            print()
            
            # Чекпойнт каждые PROGRESS_EVERY
            if processed % PROGRESS_EVERY == 0:
                print(f">>> ЧЕКПОЙНТ {processed} карточек обработано")
        
        print(f"✓ ЗАВЕРШЕНО: {processed} карточек за {elapsed/60:.1f} минут")
        
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
