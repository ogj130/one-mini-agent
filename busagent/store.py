"""SQLite persistence: the business world plus the agent's own log.

Two databases would work too; one file keeps the demo simple while preserving
the important property: business effects and agent state commit separately.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .contracts import Message, ToolCall
from .errors import NotFound
from .states import ensure_transition

BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, customer_id INT NOT NULL, status TEXT);
CREATE TABLE IF NOT EXISTS order_items(order_id INT, sku TEXT, name TEXT, qty INT);
CREATE TABLE IF NOT EXISTS shipments(
  idem_key TEXT PRIMARY KEY, order_id INT, sku TEXT, qty INT, note TEXT,
  visible INT DEFAULT 1, created REAL);
"""

RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows(
  id TEXT PRIMARY KEY, session_id TEXT, state TEXT, goal TEXT, actor TEXT,
  turns INT DEFAULT 0, tool_calls INT DEFAULT 0,
  pending TEXT, plan TEXT, answer TEXT, proposal TEXT,
  flow_id TEXT, contract TEXT, inputs TEXT, questions TEXT, coverage TEXT,
  verify_attempts INT DEFAULT 0, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, wf TEXT, seq INT, role TEXT,
  content TEXT, tool_calls TEXT, tool_call_id TEXT, name TEXT, created REAL);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, wf TEXT, digest TEXT,
  approver TEXT, active INT DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS steps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, wf TEXT, seq INT, tool TEXT, args TEXT,
  digest TEXT, idem_key TEXT, status TEXT, result TEXT, error TEXT,
  ms REAL, created REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, wf TEXT, kind TEXT, detail TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS session_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, seq INT, role TEXT,
  content TEXT, action TEXT, wf TEXT, created REAL);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(BUSINESS_SCHEMA)
        self.db.executescript(RUNTIME_SCHEMA)
        for column, ddl in (
            ("session_id", "TEXT"), ("proposal", "TEXT"), ("flow_id", "TEXT"),
            ("contract", "TEXT"),
            ("inputs", "TEXT"), ("questions", "TEXT"), ("coverage", "TEXT"),
            ("verify_attempts", "INTEGER DEFAULT 0"),
        ):
            self._ensure_column("workflows", column, ddl)
        self.fail_next_commit = False
        self._seed()
        self.db.commit()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        names = {row["name"] for row in self.q(f"PRAGMA table_info({table})")}
        if column not in names:
            self.exec(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.db.close()

    # ---- seeding -------------------------------------------------------
    def _seed(self) -> None:
        if self.q1("SELECT COUNT(*) AS n FROM customers")["n"]:
            return
        self.db.executemany("INSERT INTO customers(id, name) VALUES(?, ?)",
                            [(1, "张三"), (2, "李四"), (3, "张珊")])
        self.db.executemany("INSERT INTO orders(id, customer_id, status) VALUES(?,?,?)",
                            [(1001, 1, "已发货"), (1002, 2, "待发货"), (1003, 3, "已发货")])
        self.db.executemany(
            "INSERT INTO order_items(order_id, sku, name, qty) VALUES(?,?,?,?)",
            [(1001, "MUG-WHITE", "白瓷杯", 2), (1001, "CUP-LID", "杯盖", 2),
             (1002, "BOWL-S", "小碗", 1), (1003, "MUG-WHITE", "白瓷杯", 4)])
        self.db.commit()

    # ---- low level -----------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def exec(self, sql: str, args: tuple = ()) -> None:
        self.db.execute(sql, args)
        self.db.commit()

    # ---- workflows -----------------------------------------------------
    def create_workflow(self, wf: str, goal: str, actor: str, session: str = "") -> None:
        now = time.time()
        self.exec("INSERT INTO workflows(id, session_id, state, goal, actor, created, updated)"
                  " VALUES(?,?,?,?,?,?,?)", (wf, session, "NEW", goal, actor, now, now))

    def workflow(self, wf: str) -> sqlite3.Row:
        row = self.q1("SELECT * FROM workflows WHERE id = ?", (wf,))
        if row is None:
            raise NotFound(f"工作流 {wf} 不存在")
        return row

    def update_workflow(self, wf: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.exec(f"UPDATE workflows SET {sets}, updated = ? WHERE id = ?",
                  (*fields.values(), time.time(), wf))

    def transition(self, wf: str, new: str, **fields: Any) -> None:
        # 所有状态写入都走这里：非法迁移在落库前就被拦住。
        current = self.workflow(wf)["state"]
        ensure_transition(current, new)
        self.update_workflow(wf, state=new, **fields)

    def set_proposal(self, wf: str, proposal: str) -> None:
        self.update_workflow(wf, proposal=proposal)

    def set_contract(self, wf: str, contract: str) -> None:
        self.update_workflow(wf, contract=contract)

    def set_flow(self, wf: str, flow_id: str, contract: str, inputs: str) -> None:
        self.update_workflow(wf, flow_id=flow_id, contract=contract, inputs=inputs)

    def set_questions(self, wf: str, questions: str) -> None:
        self.update_workflow(wf, questions=questions)

    def set_coverage(self, wf: str, coverage: str) -> None:
        self.update_workflow(wf, coverage=coverage)

    def bump_verify(self, wf: str) -> int:
        self.exec("UPDATE workflows SET verify_attempts = verify_attempts + 1"
                  " WHERE id = ?", (wf,))
        return int(self.workflow(wf)["verify_attempts"])

    def bump(self, wf: str, turns: int = 0, tool_calls: int = 0) -> None:
        self.exec("UPDATE workflows SET turns = turns + ?, tool_calls = tool_calls + ?,"
                  " updated = ? WHERE id = ?", (turns, tool_calls, time.time(), wf))

    # ---- sessions ------------------------------------------------------
    def create_session(self, session: str, title: str = "") -> None:
        now = time.time()
        self.exec("INSERT OR IGNORE INTO sessions(id, title, created, updated)"
                  " VALUES(?,?,?,?)", (session, title, now, now))

    def session(self, session: str) -> sqlite3.Row:
        row = self.q1("SELECT * FROM sessions WHERE id = ?", (session,))
        if row is None:
            raise NotFound(f"会话 {session} 不存在")
        return row

    def sessions(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM sessions ORDER BY updated DESC")

    def touch_session(self, session: str, title: str = "") -> None:
        if title:
            self.exec("UPDATE sessions SET title = ?, updated = ? WHERE id = ?",
                      (title, time.time(), session))
        else:
            self.exec("UPDATE sessions SET updated = ? WHERE id = ?", (time.time(), session))

    def workflows_in_session(self, session: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM workflows WHERE session_id = ? ORDER BY created",
                      (session,))

    def append_session_message(self, session: str, role: str, content: str,
                               action: str = "", wf: str = "") -> None:
        seq = self.q1("SELECT COUNT(*) AS n FROM session_messages WHERE session = ?",
                      (session,))["n"] + 1
        self.exec("INSERT INTO session_messages(session, seq, role, content, action, wf, created)"
                  " VALUES(?,?,?,?,?,?,?)",
                  (session, seq, role, content, action, wf, time.time()))

    def session_messages(self, session: str) -> list[sqlite3.Row]:
        # action/wf 两列是审计线索：能看出当时选了哪个动作、落到哪个 workflow。

        return self.q("SELECT * FROM session_messages WHERE session = ? ORDER BY seq",
                      (session,))

    # ---- conversation log ---------------------------------------------
    def append_message(self, wf: str, message: Message) -> None:
        seq = self.q1("SELECT COUNT(*) AS n FROM messages WHERE wf = ?", (wf,))["n"] + 1
        self.exec("INSERT INTO messages(wf, seq, role, content, tool_calls,"
                  " tool_call_id, name, created) VALUES(?,?,?,?,?,?,?,?)",
                  (wf, seq, message.role, message.content,
                   json.dumps([c.__dict__ for c in message.tool_calls], ensure_ascii=False)
                   if message.tool_calls else None,
                   message.tool_call_id, message.name, time.time()))

    def messages(self, wf: str) -> list[Message]:
        # 回放的唯一来源：resume 时模型看到的就是这张表重建出来的顺序。
        out: list[Message] = []
        for row in self.q("SELECT * FROM messages WHERE wf = ? ORDER BY seq", (wf,)):
            calls = tuple(ToolCall(**c) for c in json.loads(row["tool_calls"] or "[]"))
            out.append(Message(role=row["role"], content=row["content"],
                               tool_calls=calls, tool_call_id=row["tool_call_id"],
                               name=row["name"]))
        return out

    # ---- steps ---------------------------------------------------------
    def next_step_seq(self, wf: str) -> int:
        return self.q1("SELECT COALESCE(MAX(seq), 0) AS n FROM steps WHERE wf = ?", (wf,))["n"] + 1

    def append_step(self, wf: str, seq: int, tool: str, args: dict[str, Any],
                    digest: str, idem_key: str, status: str,
                    result: Any = None, error: str = "", ms: float = 0.0) -> None:
        self.exec("INSERT INTO steps(wf, seq, tool, args, digest, idem_key, status,"
                  " result, error, ms, created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (wf, seq, tool, json.dumps(args, ensure_ascii=False), digest, idem_key,
                   status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                   error, ms, time.time()))

    def successful_results(self, wf: str) -> list[Any]:
        return [json.loads(r["result"]) for r in
                self.q("SELECT result FROM steps WHERE wf = ? AND status = 'ok'"
                       " AND result IS NOT NULL ORDER BY seq", (wf,))]

    def steps(self, wf: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM steps WHERE wf = ? ORDER BY seq", (wf,))

    # ---- approvals -----------------------------------------------------
    def approve(self, wf: str, digest: str, approver: str) -> None:
        self.exec("UPDATE approvals SET active = 0 WHERE wf = ?", (wf,))
        self.exec("INSERT INTO approvals(wf, digest, approver, active, created)"
                  " VALUES(?,?,?,1,?)", (wf, digest, approver, time.time()))

    def is_approved(self, wf: str, digest: str) -> bool:
        return bool(self.q1("SELECT 1 FROM approvals WHERE wf = ? AND digest = ?"
                            " AND active = 1", (wf, digest)))

    def approvals(self, wf: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM approvals WHERE wf = ? ORDER BY id", (wf,))

    # ---- business ------------------------------------------------------
    def find_customers(self, name: str) -> list[dict[str, Any]]:
        rows = self.q("SELECT id, name FROM customers WHERE name = ? OR name LIKE ?",
                      (name, f"%{name}%"))
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def find_order(self, order_id: int, customer_id: int) -> dict[str, Any]:
        row = self.q1("SELECT id, customer_id, status FROM orders"
                      " WHERE id = ? AND customer_id = ?", (order_id, customer_id))
        if row is None:
            raise NotFound(f"订单 {order_id} 不属于客户 {customer_id}")
        return {"order_id": row["id"], "customer_id": row["customer_id"],
                "status": row["status"]}

    def missing_items(self, order_id: int) -> list[dict[str, Any]]:
        rows = self.q("SELECT sku, name, qty FROM order_items WHERE order_id = ?",
                      (order_id,))
        if not rows:
            raise NotFound(f"订单 {order_id} 没有缺件记录")
        return [{"sku": r["sku"], "name": r["name"], "qty": r["qty"]} for r in rows]

    def insert_shipment(self, idem_key: str, order_id: int, sku: str, qty: int,
                        note: str, visible: bool) -> dict[str, Any]:
        self.exec("INSERT OR IGNORE INTO shipments(idem_key, order_id, sku, qty, note,"
                  " visible, created) VALUES(?,?,?,?,?,?,?)",
                  (idem_key, order_id, sku, qty, note, int(visible), time.time()))
        row = self.q1("SELECT * FROM shipments WHERE idem_key = ?", (idem_key,))
        return {"shipment_id": f"SHP-{idem_key[:8].upper()}", "idem_key": idem_key,
                "order_id": row["order_id"], "sku": row["sku"], "qty": row["qty"]}

    def find_shipment(self, idem_key: str) -> dict[str, Any] | None:
        row = self.q1("SELECT * FROM shipments WHERE idem_key = ? AND visible = 1",
                      (idem_key,))
        return None if row is None else {"shipment_id": f"SHP-{idem_key[:8].upper()}",
                                         "idem_key": idem_key, "order_id": row["order_id"],
                                         "sku": row["sku"], "qty": row["qty"]}

    def shipment_count(self, visible_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM shipments"
        if visible_only:
            sql += " WHERE visible = 1"
        return self.q1(sql)["n"]

    # ---- events --------------------------------------------------------
    def event(self, wf: str, kind: str, detail: str = "") -> None:
        self.exec("INSERT INTO events(wf, kind, detail, ts) VALUES(?,?,?,?)",
                  (wf, kind, detail[:400], time.time()))

    def events(self, wf: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM events WHERE wf = ? ORDER BY id", (wf,))
