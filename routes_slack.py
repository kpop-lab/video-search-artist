# routes_slack.py
from flask import Blueprint, request, jsonify
import json
import re
import requests

from config import SLACK_API_KEY, search_model
from utils import get_db_connection

slack_bp = Blueprint('slack_bp', __name__)

@slack_bp.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    payload_str = request.form.get("payload")
    payload = json.loads(payload_str)

    # 블록 액션 이벤트 처리: 수정 버튼 클릭
    if payload["type"] == "block_actions":
        action = payload["actions"][0]
        if action["action_id"] == "modify_click":
            segment_id = action["value"]   # 예: "141"
            trigger_id = payload["trigger_id"]

            open_modal_view = {
                "trigger_id": trigger_id,
                "view": {
                    "type": "modal",
                    "callback_id": "modify_modal_submit",
                    "private_metadata": segment_id,  # 모달 제출 시 세그먼트 ID 전달
                    "title": {"type": "plain_text", "text": "자막 수정"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "caption_input_block",
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "caption_input",
                                "placeholder": {"type": "plain_text", "text": "새 자막을 입력하세요"}
                            },
                            "label": {"type": "plain_text", "text": "자막"}
                        }
                        # (슬랙 모달에는 현재 캡션만 입력받지만, 나중에 멤버 선택도 추가할 수 있음)
                    ],
                    "submit": {"type": "plain_text", "text": "완료"}
                }
            }

            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {SLACK_API_KEY}"
            }
            response = requests.post(
                "https://slack.com/api/views.open",
                headers=headers,
                data=json.dumps(open_modal_view)
            )
            print("views.open response:", response.json())
            return "", 200
        else:
            return "", 200

    # 모달 제출 이벤트 처리: 자막 수정 제출 (슬랙 모달로 입력된 경우)
    elif payload["type"] == "view_submission" and payload["view"]["callback_id"] == "modify_modal_submit":
        segment_id = payload["view"]["private_metadata"]
        new_caption = payload["view"]["state"]["values"]["caption_input_block"]["caption_input"]["value"]

        try:
            seg_id = int(segment_id)
            new_emb = search_model.encode(new_caption)
            new_emb_str = str(new_emb.tolist())
            conn = get_db_connection()
            cur = conn.cursor()
            update_sql = """
            UPDATE njz_segments
            SET manual_caption = %s,
                embedding = %s
            WHERE id = %s
            """
            cur.execute(update_sql, (new_caption, new_emb_str, seg_id))
            conn.commit()
            cur.close()
            conn.close()
            print(f"세그먼트 {seg_id} 자막 수정 완료")
        except Exception as e:
            print("DB 업데이트 에러:", e)
        return jsonify({"response_action": "clear"}), 200

    # 그 외의 경우 빈 응답 반환
    else:
        return "", 200


@slack_bp.route("/slack/playvideo", methods=["POST"])
def slack_video():
    user_text = request.form.get("text", "").strip()

    # 1) "수정: 세그먼트ID=xx 내용=... 멤버=..." 명령 처리
    if user_text.startswith("수정:"):
        override_pattern = r'^수정:\s*세그먼트ID=(\d+)\s*내용=(.*?)(?:\s+멤버=(.*))?$'
        match = re.match(override_pattern, user_text)
        if match:
            seg_id_str = match.group(1)
            override_text = match.group(2).strip()
            members_text = match.group(3)  # 선택적으로 있을 수 있음

            try:
                seg_id = int(seg_id_str)
            except ValueError:
                seg_id = None

            if not seg_id or not override_text:
                return jsonify({"response_type": "ephemeral", "text": "교정 명령 구문이 잘못되었습니다."})

            # 1) 수정 전 데이터 조회
            conn = get_db_connection()
            cur = conn.cursor()
            select_sql = """
            SELECT caption, manual_caption, faces
            FROM njz_segments
            WHERE id = %s
            """
            cur.execute(select_sql, (seg_id,))
            row = cur.fetchone()
            if not row:
                cur.close()
                conn.close()
                return jsonify({"response_type": "ephemeral", "text": f"세그먼트 {seg_id}를 찾을 수 없습니다."})
            old_auto_cap = row[0] or ""
            old_manual_cap = row[1] or ""
            old_faces = row[2] or "[]"
            old_final_cap = old_manual_cap.strip() if old_manual_cap.strip() else old_auto_cap
            if isinstance(old_faces, str):
                try:
                    old_faces = json.loads(old_faces)
                except:
                    old_faces = []

            # 2) 새 faces 만들기 (멤버 정보)
            if members_text:
                members_list = [m.strip() for m in members_text.split(",") if m.strip()]
                new_faces = json.dumps([{"member": m} for m in members_list])
            else:
                new_faces = json.dumps([])

            # 3) DB 업데이트
            new_emb = search_model.encode(override_text)
            new_emb_str = str(new_emb.tolist())
            if members_text:
                update_sql = """
                UPDATE njz_segments
                SET manual_caption = %s,
                    embedding = %s,
                    faces = %s
                WHERE id = %s
                """
                cur.execute(update_sql, (override_text, new_emb_str, new_faces, seg_id))
            else:
                update_sql = """
                UPDATE njz_segments
                SET manual_caption = %s,
                    embedding = %s
                WHERE id = %s
                """
                cur.execute(update_sql, (override_text, new_emb_str, seg_id))

            # 4) 히스토리 기록
            hist_sql = """
            INSERT INTO njz_segment_operations_history
            (operation_type, segment_ids_before, segment_ids_after,
             old_captions, new_captions, old_faces, new_faces, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            cur.execute(hist_sql, (
                "update",
                json.dumps([seg_id]),
                json.dumps([seg_id]),
                json.dumps([old_final_cap]),
                json.dumps([override_text]),
                json.dumps([old_faces]),
                json.dumps([json.loads(new_faces)]),
                "local_user"
            ))

            conn.commit()
            cur.close()
            conn.close()

            return jsonify({
                "response_type": "in_channel",
                "text": f"세그먼트 {seg_id} 캡션 및 멤버 정보가 수정되었습니다."
            })
        else:
            return jsonify({"response_type": "ephemeral", "text": "수정 명령 형식이 올바르지 않습니다."})

    # 2) 수정 명령이 아닐 경우, 검색 로직 수행
    else:
        user_emb = search_model.encode(user_text)
        user_emb_str = str(user_emb.tolist())

        conn = get_db_connection()
        cur = conn.cursor()
        sql = """
        SELECT
            id, video_id, start_time, end_time, caption, manual_caption, faces,
            embedding <=> %s AS distance
        FROM njz_segments
        ORDER BY embedding <=> %s
        LIMIT 3;
        """
        cur.execute(sql, (user_emb_str, user_emb_str))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        blocks = []
        if not rows:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "DB에서 검색 결과가 없습니다."}
            })
        else:
            from urllib.parse import quote_plus
            encoded_query = quote_plus(user_text)
            for row in rows:
                seg_id = row[0]
                # GIF 파일이 존재하는 세그먼트만 처리 (예: 272 이하)
                if seg_id > 272:
                    continue

                start_time = row[2]
                end_time = row[3]
                auto_cap = row[4] or ""
                manual_cap = row[5] or ""
                final_cap = manual_cap.strip() if manual_cap.strip() else auto_cap
                time_slot = f"{int(start_time)}-{int(end_time)} sec"

                section_block = {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*세그먼트ID={seg_id}* (start={start_time})\n{time_slot}: \"{final_cap}\""
                    }
                }
                image_block = {
            "type": "image",
            "image_url": f"https://f2b8-58-72-151-123.ngrok-free.app/gif/output_segment_{seg_id:04d}.gif",
            "alt_text": f"세그먼트 {seg_id} GIF"
        }
                actions_block = {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "영상 보러가기"},
                            "url": f"https://f2b8-58-72-151-123.ngrok-free.app/video_player?t={start_time}"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "수정하기"},
                            "value": str(seg_id),
                            "action_id": "modify_click"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Edit in Web"},
                            "url": f"https://f2b8-58-72-151-123.ngrok-free.app/?q={encoded_query}"
                        }
                    ]
                }
                blocks.extend([section_block, image_block, actions_block])

        slack_response = {
            "response_type": "in_channel",
            "blocks": blocks
        }
        return jsonify(slack_response)
