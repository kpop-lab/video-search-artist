import json
import psycopg2
from sentence_transformers import SentenceTransformer

# 1. 모델 로드 (Hugging Face 모델)
model = SentenceTransformer("bongsoo/kpf-sbert-128d-v1")

# 2. JSON 데이터 불러오기 (combined metadata)
json_path = "data/combined_metadata.json"
with open(json_path, "r", encoding="utf-8") as f:
    combined_data = json.load(f)

# 3. PostgreSQL 연결
conn = psycopg2.connect(
    dbname="postgres",
    user="postgres",
    password="1234",
    host="localhost",
    port="5432"
)
cur = conn.cursor()

# 4. pgvector 확장 및 테이블 드롭 후 재생성
create_table_sql = """
CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS njz_segments;
CREATE TABLE njz_segments (
    id SERIAL PRIMARY KEY,
    video_id TEXT,
    start_time FLOAT,
    end_time FLOAT,
    caption TEXT,
    manual_caption TEXT,
    faces JSONB,
    embedding vector(128),
    UNIQUE(video_id, start_time, end_time)
);
"""
cur.execute(create_table_sql)
conn.commit()

# 5. INSERT OR UPDATE 쿼리
insert_sql = """
INSERT INTO njz_segments (video_id, start_time, end_time, caption, faces, embedding)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (video_id, start_time, end_time)
DO UPDATE SET
    caption = EXCLUDED.caption,
    faces = EXCLUDED.faces,
    embedding = EXCLUDED.embedding;
"""

# 6. 데이터 처리 및 삽입
for seg in combined_data:
    video_id = seg.get("video_id", "FuJ1RiLoq-M")
    # combined metadata는 1초 단위이므로, timestamp를 시작시간으로 사용하고 종료시간은 start_time + 1초
    timestamp = seg.get("timestamp", 0)
    start_time = float(timestamp)
    end_time = start_time + 1.0

    caption = seg.get("caption", "")
    # faces는 combined metadata에서 이미 여러 얼굴 정보를 리스트로 포함하고 있음
    faces = seg.get("faces", [])
    faces_str = json.dumps(faces, ensure_ascii=False)

    # 캡션 임베딩 계산 (128차원 벡터)
    embedding = model.encode(caption)
    embedding_str = str(embedding.tolist())

    cur.execute(insert_sql, (video_id, start_time, end_time, caption, faces_str, embedding_str))
    conn.commit()

# 7. 연결 종료
cur.close()
conn.close()

print("✅ Embeddings inserted or updated into PGVector table 'njz_segments' successfully.")
