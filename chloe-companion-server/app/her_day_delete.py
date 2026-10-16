from fastapi import Request, HTTPException
from pydantic import BaseModel

class HerDayDeleteIn(BaseModel):
    media_url: str | None = None
    post_id: int | None = None

def register_her_day_delete(app, _require_admin, _conn):
    @app.post("/api/v1/admin/her-day/delete")
    def admin_her_day_delete_post(body: HerDayDeleteIn, request: Request):
        _require_admin(request)
        if body.post_id is None and not body.media_url:
            raise HTTPException(status_code=400, detail="pass post_id or media_url")
        conn = _conn()
        try:
            if body.post_id is not None:
                cur = conn.execute("DELETE FROM her_day_posts WHERE id = ?", (body.post_id,))
            else:
                cur = conn.execute("DELETE FROM her_day_posts WHERE media_url = ?", (body.media_url,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="post_not_found")
            return {"ok": True, "deleted": cur.rowcount}
        finally:
            conn.close()
