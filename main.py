import random
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
import sys
import os

# 在文件开头添加这个辅助函数
def resource_path(relative_path):
    """ 获取资源文件的绝对路径，兼容开发环境和 PyInstaller 打包后环境 """
    try:
        # PyInstaller 创建临时文件夹，将路径存储在 _MEIPASS 中
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)
app = FastAPI()

# ----------------- 棋盘逻辑（不变） -----------------
def init_board():
    return [[[None for _ in range(3)] for _ in range(3)] for _ in range(9)]

def is_sub_board_full(sub_board):
    for row in sub_board:
        if None in row:
            return False
    return True

def check_sub_board_win(sub_board):
    lines = []
    for r in range(3):
        lines.append([sub_board[r][c] for c in range(3)])
    for c in range(3):
        lines.append([sub_board[r][c] for r in range(3)])
    lines.append([sub_board[i][i] for i in range(3)])
    lines.append([sub_board[i][2 - i] for i in range(3)])
    for line in lines:
        if line[0] is not None and line[0] == line[1] == line[2]:
            return line[0]
    return None

def make_move(board, sub_board_owners, sub, row, col, color):
    if board[sub][row][col] is not None:
        return False, "这个格子已经有棋子了"
    if sub_board_owners[sub] is not None:
        return False, "这个小九宫格已经被占领了"
    if is_sub_board_full(board[sub]):
        return False, "这个小九宫格已经满了"
    board[sub][row][col] = color
    winner = check_sub_board_win(board[sub])
    if winner:
        sub_board_owners[sub] = winner
    return True, None

def calc_occupied_count(sub_board_owners):
    counts = {"white": 0, "black": 0}
    for owner in sub_board_owners:
        if owner == "white":
            counts["white"] += 1
        elif owner == "black":
            counts["black"] += 1
    return counts

def is_game_over(sub_board_owners, board):
    for i in range(9):
        if sub_board_owners[i] is None and not is_sub_board_full(board[i]):
            return False
    return True

# ----------------- 房间管理 -----------------
rooms = {}
waiting = []
ws_to_room = {}

def get_room(ws):
    rid = ws_to_room.get(ws)
    return rooms.get(rid)

@app.websocket("/ws")
async def game_endpoint(ws: WebSocket, username: str = Query("玩家")):
    await ws.accept()

    if waiting:
        opponent_ws, opp_name = waiting.pop(0)
        if random.choice([True, False]):
            white_ws, black_ws = opponent_ws, ws
            white_name, black_name = opp_name, username
        else:
            white_ws, black_ws = ws, opponent_ws
            white_name, black_name = username, opp_name

        room_id = f"{white_name}_vs_{black_name}"
        rooms[room_id] = {
            "white_ws": white_ws,
            "black_ws": black_ws,
            "white_name": white_name,
            "black_name": black_name,
            "board": init_board(),
            "sub_board_owners": [None] * 9,
            "turn": "white",
            "next_sub_board": None,
            "game_over": False          # 新增加游戏结束标记
        }
        ws_to_room[white_ws] = room_id
        ws_to_room[black_ws] = room_id

        await white_ws.send_json({
            "type": "game_start", "color": "white", "opponent": black_name
        })
        await black_ws.send_json({
            "type": "game_start", "color": "black", "opponent": white_name
        })
    else:
        waiting.append((ws, username))
        await ws.send_json({"type": "waiting", "message": "等待对手加入..."})
        try:
            while get_room(ws) is None:
                await ws.receive_text()
        except WebSocketDisconnect:
            waiting[:] = [(w, n) for w, n in waiting if w != ws]
            return

    room = get_room(ws)
    if room is None:
        return

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") != "move":
                continue

            # 游戏结束后拒绝落子
            if room.get("game_over"):
                await ws.send_json({"type": "error", "msg": "游戏已结束"})
                continue

            sub = data.get("sub_board")
            row = data.get("row")
            col = data.get("col")
            if sub is None or row is None or col is None:
                continue
            if not (0 <= sub <= 8 and 0 <= row <= 2 and 0 <= col <= 2):
                await ws.send_json({"type": "error", "msg": "落子位置无效"})
                continue

            if room["white_ws"] == ws:
                color = "white"
            else:
                color = "black"

            if room["turn"] != color:
                await ws.send_json({"type": "error", "msg": "还没轮到你"})
                continue

            next_sb = room["next_sub_board"]
            if next_sb is not None:
                target_owner = room["sub_board_owners"][next_sb]
                target_full = is_sub_board_full(room["board"][next_sb])
                if target_owner is None and not target_full:
                    if sub != next_sb:
                        await ws.send_json({"type": "error", "msg": f"必须下在第 {next_sb+1} 个小九宫格"})
                        continue

            success, err = make_move(room["board"], room["sub_board_owners"], sub, row, col, color)
            if not success:
                await ws.send_json({"type": "error", "msg": err})
                continue

            next_candidate = row * 3 + col
            if room["sub_board_owners"][next_candidate] is not None or is_sub_board_full(room["board"][next_candidate]):
                room["next_sub_board"] = None
            else:
                room["next_sub_board"] = next_candidate

            room["turn"] = "black" if color == "white" else "white"

            update = {
                "type": "board_update",
                "board": room["board"],
                "sub_board_owners": room["sub_board_owners"],
                "occupied_counts": calc_occupied_count(room["sub_board_owners"]),
                "turn": room["turn"],
                "next_sub_board": room["next_sub_board"],
                "last_move": {"sub_board": sub, "row": row, "col": col, "color": color}
            }
            await room["white_ws"].send_json(update)
            await room["black_ws"].send_json(update)

            # 检查游戏是否结束
            if is_game_over(room["sub_board_owners"], room["board"]):
                counts = calc_occupied_count(room["sub_board_owners"])
                if counts["white"] > counts["black"]:
                    winner = "white"
                elif counts["black"] > counts["white"]:
                    winner = "black"
                else:
                    winner = "draw"

                room["game_over"] = True   # 标记结束
                end_msg = {
                    "type": "game_over",
                    "winner": winner,
                    "occupied_counts": counts
                }
                await room["white_ws"].send_json(end_msg)
                await room["black_ws"].send_json(end_msg)
                # 不 break，继续循环保持连接

    except WebSocketDisconnect:
        # 断线处理：仅在游戏未结束时通知对手获胜
        if room and not room.get("game_over"):
            other_ws = room["white_ws"] if room["black_ws"] == ws else room["black_ws"]
            winner_color = "white" if room["white_ws"] == other_ws else "black"
            await other_ws.send_json({
                "type": "game_over",
                "winner": winner_color,
                "reason": "对手断开连接",
                "occupied_counts": calc_occupied_count(room["sub_board_owners"])
            })
            # 标记结束，防止对方再操作
            room["game_over"] = True
        # 清理房间映射
        rid = ws_to_room.get(ws)
        if rid:
            r = rooms.get(rid)
            if r:
                other = r["white_ws"] if r["black_ws"] == ws else r["black_ws"]
                if other in ws_to_room:
                    del ws_to_room[other]
            if rid in rooms:
                del rooms[rid]
    finally:
        if ws in ws_to_room:
            del ws_to_room[ws]

@app.get("/")
async def index():
    # 使用 resource_path 获取 index.html 的正确路径
    return FileResponse(resource_path("index.html"))

if __name__ == "__main__":
    import traceback
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
    except Exception as e:
        print(f"启动失败: {e}")
        traceback.print_exc()
        input("按回车键退出...")