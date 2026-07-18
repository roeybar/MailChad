"""Minimal admin auth for vault.

Single-operator model (PRD-faithful with v1/v2). Email + bcrypt-hashed
password in env, JWT cookie session.

ADMIN_EMAIL: plaintext email/handle
ADMIN_PASSWORD_HASH: bcrypt hash (generate with bin/v3 admin-bcrypt)
JWT_SECRET: base64-48

Ported in spirit from email-platform/src/lib/auth/.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import base64

import bcrypt
from fastapi import APIRouter, Cookie, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from mailchad.terminal import db, settings

log = logging.getLogger("terminal.auth")
router = APIRouter()

SESSION_COOKIE = "v3_session"


def _admin_email() -> str:
    return settings.get("admin_email", "") or ""


def _admin_password_hash() -> str:
    return settings.get("admin_password_hash", "") or ""


def _jwt_secret() -> str:
    return settings.get("jwt_secret", "") or ""


def _session_ttl_s() -> int:
    return settings.get_int("session_ttl_s", 86400)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload: dict) -> str:
    secret = _jwt_secret()
    if not secret:
        raise RuntimeError("JWT_SECRET not set in settings - set via admin UI")
    payload_b = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = _b64u(payload_b)
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u(sig)}"


def _verify(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    payload_b64, sig_b64 = token.split(".", 1)
    secret = _jwt_secret()
    if not secret:
        return None
    expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64u_decode(sig_b64), expected):
        return None
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def require_session(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """FastAPI dep: requires a valid session cookie."""
    payload = _verify(session) if session else None
    if not payload:
        raise HTTPException(
            status.HTTP_307_TEMPORARY_REDIRECT,
            "auth required",
            headers={"Location": f"/admin/auth/login"},
        )
    return payload


def require_admin(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """FastAPI dep: requires a valid session with admin role."""
    payload = require_session(request, session)
    if payload.get("role", "admin") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return payload


# Routes

def login_page(error: str | None = None) -> HTMLResponse:
    err_html = (
        f'<div class="callout crit" style="margin-bottom:16px">'
        f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="width:17px;height:17px;flex-shrink:0;margin-top:1px"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
        f'<div class="ct-body">{error}</div></div>'
    ) if error else ""
    mail_svg = '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAb7UlEQVR42sV7a2xc55ne833nMmdmzsxwOBdqSCmSGEqiSEqRI18iObKluErS2HB3HUjI2o43QgMDdREnAbYBtjeKRYqmP5pkt6mRNbIwnBhOSytpE6+zG8swSSe2INO1LStSHUkWKYriReTMcC7nzJlz+/ojfL89ZOTYcRp0gIGtGc7MeW/P+7zP9x6GP+AhhGDHjx9XRkZGQgBh9D3DMNBqtQpLS0sb5+bmsmEY8lqtZk5PT2+anp7eury83O04TrfruiXbtrNCCFVVVaiq2gZgAxCMMZFMJqulUuntnTt3vnL33Xf/slQqvdVsNuXvHDlyRBkdHRWMsfCD2MA+qPGjo6PK0aNHAwDgnCMIgvT09PSHJycnd01NTe29fPnyTSsrK/3Ly8u5IAh4EARwHAf1eh2tVgue58H3fYRhCM45OOfQNA2KogAAXNdFEARQVRWGYaCzsxNdXV3u1q1bz/b394997GMf+/u9e/e+xhirA8Dw8LAKIFwNxh/PAVHDhRDm+Pj4n42Pj//p+fPnb5qfn9+wsrICejqOA8YYgiAQqqrC932RSqVELBZDGIbQdZ0xxpimadA0DaqqIgxD+L4PIQR0XQdjTAAQrVaLBUHAASCVSqFUKqGrq2vu5ptvfvGrX/3qf2KMnadrHB4eVo8fPx6sfvb/jQOEEIwxxgCEQghlcnLyz3/0ox/95cTERN/FixfRbrcRBAEURQmEEEJRFBaLxbjv+0zXdXoP6XQaruuCcw7P8xCPx5FIJBCGIRzHged5UBQFiqIgk8nA933U63W0221kMhnBGBP1ej30PI/7vs87Ozvx6U9/unH48OH/fPfdd/8P0zQvWZb1W8H6gxywarxgjOFXv/rVnzz99NP/dmJiYu+ZM2fg+75vmibjnHNVVZnneRBCYDXiUFUVjDG0Wi3kcjn5nb7vw7ZtFAoFhGEIwzBQr9fxGx8DPT09EEJgfn4ejuOgo6MDQRCg2WxSZoBzLmq1WhAEgdrf34/u7u72pk2bzu/Zs+eFBx544FuMsfnh4WF1ZGTE/8AOWAU6dvz4cfWJJ5747okTJ45NTEwgDMPANE2maRqn6BuGAcdxoOs6NE2D53nQdR2NRkM6gnMOIQR830cymYQQApqmgXMO3/cRj8dRKBRgGAauXbsGx3GQTqdhWRbCMEQ2m0W73Uaj0QDhD+dcCCFCAArnHF1dXfjkJz85+/DDD3++VCqN/y4nKO/lgMHBQeXRRx8NBwYGvvf4448fe+mll/x0Og3TNBXP85iiKLJm6REEAVzXlZEOggCMMRndMAyhaRoAIBaLgb4jkUgglUrJtCcQZIxJsLQsC81mE0II6UwhBOOc8zAMRbvdFouLi/4bb7yRXVxc/Pxjjz228OCDD04ODw+rExMT4e+VAVRDzz///JHvfve7oy+//LKbTCY113UZpbVhGGCMSYOFEAjDEEIIMMZkPXPOoev6mtfpNV3XoarqbzmMAFFRFLiuKx1N3xmGITzPQ6vVgu/7sv3qug7f98NarYZ77rmHHzt27NG77rrrv94IE9R3M354eJgfPXoUQoiOL3/5y391+vTpMJfLqa7rMkVR4HmeNF5RFBiG8Y9ejRiuqqpsb1QCZHAYhjILyOAgCOB5HjzPQxiG8hmLxWAYBnzfh+/7cF1XOoUMZ4zJv9M0jafTaXHy5ElfUZS/Hh8fXzh48OAz652gvgfiB0899dQ3X3755VI8Hg96enoUxhhs20YQBLKPR1ObjAQgnUDRAgBd10Ft0PM8aJoGXdfBOZdO8TwP7XYbruuCnO37PjzPwypZgqIo0HVdvkZcgvBltXxYPp/nv/jFL8SHP/zhvxFCPH/8+PEGgfq7OYAxxpiqqsGzzz478p3vfOfY0tJSsHv3biWVSkEIAdM00W63JTABAKE/OYMAijCBsoEMAgBN02TECCMIFA3DgOd5kjy5roswDEEtjiK/SsIkrpBTHMdBEATQdZ27rutPTk5mT548+eWRkZH/sGq3f0MHDA8Ps+PHj+P73//+U9/+9rcfOHfuXLB//34lmUzCdV3pcQAyE25U+xQNigx9hoCNjI/FYvKz5AD6nKqqMmMcx5EpT78TBIEEX+oivu+Dcy6/y7ZtCCGUkydPhps3b/5LIcQoY+xtIQRnjIXKOuPVkZGR4FOf+tTnf/jDH/77iYkJb//+/Womk5E1TC3P931JaaMdgCJJ0eacy8+SYRRh6ueURfS35BByLBGnVVYp36NyME0T6XQa6XQasVhMOo5+PxaLMUVRwtnZWV0I0fvCCy88HYYhm5iYEOszIOSc46233nr47Nmz4c0338w7OzsRhiEURYHjOJKxUaSIu1M0KAKUmlT/5CQyXFVVWQpkdNSJFFFK5TAMkUgkZIfwPE9mE80OFAwKEGUnYwzJZFK5fv16ODk5+RnP83Yzxt4aHR1VeBT1R0ZGwiAIet955529sViM9fb2ckVREI/HYVkWXNeVUXddF+12W2ZCu91Gu92WqUwoTp+h1KX6pc8SuEVnAIocRdwwDJimKVsmZYumaRBCSMCkayLHx2IxcM7lNRmGEdZqNZw6depTAFihUGDSAQcPHuRCCDY5OXnnlStXjI0bNwadnZ2MWk90ehNCwHEc+QOUuvRfmU6rP0xkh9Caviua9jcqFzJGVVWJLalUCqZpIplMyqeqqrJ9CiFkgAAgkUhI4I3FYiiXy5idnf0YAPHYY4/9YwmMj4/j0KFD4sSJEzcHQYBt27bJaJEXKUK+70PXdZimiVqttga4iANQShPNpfdpPoiCJTmLGKGmaUgkEhJHLMuSZUCGENbQv1fJj8QLKgnChFXHKwsLC3jnnXcOCCHSjLG6Gun7vhAi/Y1vfOOfua6LdDrNr127Bs/zkEwm4XkeISo450in07IXt1ot+ToRFzI6WhZRFqfrOuLxODKZDFRVlSVBNb0KXjAMA9lsFtVqFdVqFUEQrOn35Mj13YAcQqRqtfOwZrMZXrhwoTAzM3MrgBdUAHjmmWc4gODq1av7Z2dne+LxeGjbNqcPN5tNqKqKSqWCRCKB7u5ueRHxeBy1Wg2tVktmAF0IpSIBZBiGSKVSEhgVRcHKyorsCsT7o1hAiF4sFpFOp9FsNlGv19fMHNHOoWma/D2i0HQtq+UZLi8vs+np6SHpgEKhwABgeXm5JwgCUSgUQkVReCqVwuLioqwloqrxeBzXr19Hu90G51zWKqUdgR/hBaU8gR+ldq1WQywWQyKRgGmaSCQSSCaTawCQniSadHR0QNM0uK4rOwSVUJQQUblQVlKZBkEA27aZbdsfWkOERkdHFc/z/I6ODkZEhVSaRqOBXC6HdDqNRqOBqampNSXhuq4kHeSMKPUlxCY8oO9VFAVBEMCyLNlNaEzWNE0OQtT6qAOk02kwxrC4uIharSYdQSVEWRCh9tE2y6rVKur1ei8AqKvzPh8ZGXFffPHFQcMwJM21bRuO42B+fh4AkMvlUKvVsLi4KHtys9mE67oSlKgLkCZAEaa6X4/49D2u68q2Zts2DTS/xTBpXiAlqdVqyYyjVI+SqCi3iAJxGIZZAFBXhwL32rVrf/rkk08+MjU1FWYyGWV6ehrVahW2bcO2bdnK0um0VHMIwQmshBCwLGvNrE79mcCRGJymaUgmk8hkMkgkEnJAymazMp3J2OiUSACraRpM00Sj0ZCkiOqcOldUZG21Wkgmk2i1Wmu6jyqE2PDjH//4m9/61rf+7MyZM2CMiTfffJNRVCk92+02Ojs7sWHDBhklzjkMw5ARrdVq8H1ftie6MIpgPB5HMpmUIOU4DsrlMpLJJDo7O6FpGhzHQSKRQCaTWVNK0SwxDEOWRTweR6PRWJPyVDY0UOm6jkKhgI6ODkxNTYkPfehDKBaLvxRCMPUnP/nJ3544ceIzZ8+eDWKxGC+Xy4x096iK43kecrkcent7YVkWYrEYOjs7wRhDo9FAuVxGu91ew9+jpMgwDKRSKTkAGYaxJjPoOzVNQzweh+u6sr+TE6IlRq2VyoxkN4p6Op2WBKmnpwemaeLtt99GIpHAzp07kc/nLzPGhLqwsHDLpUuXAl3XuWVZjDEm6zI6pJACFIvFkEwm4TiORG5N09But1Eul9eAIhlAtJRGXeIHRFCiKB+LxdaM08TuCDuom1ApkBhDUpqu6+jp6UE8HpejexiG2LRpExhjePXVV/krr7yCoaGhfyGEeFoNw1CNxWKcMYahoSGEYYhz586hWq3KHk7/DYIAy8vLsG1bepeiTshOkSKDXdeVf0sARBNgFCjj8biMYHS4IaCMBiJKl+n9MAxh2zY0TUMul5MDVxAEmJ+fR71eR7FYxK5du/jk5KR488039x4+fPifqOl0ei6Xy2UNw/CFEOobb7yBcrm8RlRIpVJot9vo7+9HuVyWbZFSmPAik8lI3IgKoVEkD8MQqqpKo3Vdl4hPT0rzaMskY6mXU2kqiiL5SFQt0jRNBqJYLKJer6NcLsMwDHR0dIS//vWv+YULF25Th4aG/uLixYv/6/Tp07GFhQVx7do1xhhDd3c3FhYWZHvJZDLo7OzEwsICDMNY84N0cR0dHTLl2+32mnGX0J+4OaVuFBPIeOILlE1U82QotcSoc4MgQKvVIqIjs+XSpUtrskEIge7ubra0tMQuXrx4n7pnz55/mJycvMM0zadOnTq1rVKphKZp8mKxKNnaysoKdu7cKUuBBhUyjnMO0zTRbDZhGAbi8bic9ijdCQciIoW8sKhoSu9HmRsBHn0n4YHrumg0Gmi1WvL0KJfLQdM01Go1ZDIZNJtNXL9+HYRtq7/LstkshBBZZWxsTL399tuvPv/88/9QLpf/PAiC2MDAAIQQbH5+Ho1GA4lEAoODg6hUKjAMA4lEQgobdMGpVAq6rqPZbEo2GFWHKQuIzFBkSfKiqEfFjCiDi1JearO+76PZbKJSqUgs2rhxI/L5PCzLAucc+XweHR0dsG1bOmtpaUnk83l26NChSX7o0CF/dHRU4ZxfzGazc/F4nE1NTYm5uTkIIZBMJjE0NISVlRX4vi/TNqrrqaoqabDv+2g0GrIEovJ2NKpRUcWyLFiWJUuG0j2qG6yX1qkrkeFElSko1KFyuRwymQyy2azsXpZliWw2i46OjndU0slff/31w6+99tq2SqUSWpbF2+02DMNAb28vMpkMpqenpfhA6E4XTeMwzfG5XA7Xr1+XogSxuqjgQRlALZOkcMqIWCwmjV9/skTnjo7jrDmVIh2AnEhiaiaTQS6Xw+LiIoIgwObNm1EsFoXv+1W1Wq1yAMGVK1c+HYah0tvb68/MzHBK2927d+PcuXMyJTnnsmVFGWI6nZYMkdoXSdSU1jRVEpMj1CcgazabcpCh0qLsaTabslRqtZocxOiEWdd1uK6LWq2Ger2OZDIJy7IwNzcHx3EQj8dRKpVQKpUoi1gul7uizs3NBatn7nsqlQpisRgj/T+qAkUlpzAM0Wq10Gg0UK1W0Ww20dHRgb6+PjiOI3UDUndIs6PBJZ/Py95NqK8oCpaWliSdpnKh6dDzPNTrdaRSKXieJ2eAeDwuCVOr1ZKl2NnZKfFB13Xk83nYto1WqxW2Wi3W29vb3L1797PKxMSEmJ2d3VQul79pWRYPw5D5vs8sy8KlS5eQz+flCS+pwa1WS57cOo4jU3tpaQmzs7NrACw6p1PEGo2GNIyk7FXlFsvLy2g2mxIISYyl9KbSo981TROcc/mdnHNZ70EQIJlMIpvNUlYGiUQCn/jEJ5Rbb731kU2bNo2rAJiiKJVkMjm3devWTe12W0xNTbHZ2VksLS1hcnISpmnKC0wmk6hWq3LgoMOKRqMhkTda51HEp1oVQqBcLqNer6NUKqG3t1dmSjqdxtWrVxGGIZLJJACgXq/Lz0aZZ1dXlwRCAkbGmGx9q/wkZIwJ0zSVQqGgDA0NYXBw8NE9e/b87ejoqKKOjY0ppVLJeumll/4GwH/MZDKB67q80WhA13VcuXIFAwMD2LBhAyqVCorFIhYXFzEzMyNbV/Qcj+qWLoi0OSoneSi5Skymp6ehKAp27Ngh05pOm2lxgnOO5eVlJBIJKYpET5qjErmu67Asi2RwUSgUeFdXF3K5nLt9+/bLO3fu/IstW7Y8R+CvHjx4MACAAwcO/Lcnnnjiq7Zt55PJpOjp6WHNZhPFYhGJRAJzc3Ny8ovs+MjeTawsOgeQpkfgSeNplCcAQKVSkRdNXSF63NZut2XG0TxBbJN+J8oPHMeBYRhicHCQffSjH31148aNf7Vv375JANOMMS96QqwyxsSRI0cUxljt8ccfPz09PX23aZpBNptVPc9DPp+XLadQKGBlZQVLS0uypkndpQuMSmkUGZLXLcuSsztFmi6chFXCDuoelC3UlXzfRyqVks7TNA3NZlO2O1VVsbCwEIZhyG655Za377vvvjsZY86NlrwAgAPAI488wgCwUqk0RiDneR4SiQRc10W9XpcnuKqqYsOGDSgWiyI6zhIARcXPIAhQq9Vkt1hYWMD8/DyazaZsb0RabNuWHSV6UhQVVYiHkFTnOI5snfl8PjpShx0dHUzX9V8yxpyf/exnseHhYS6EYOsXJPjqqVAIQNxzzz0/zWQy7ZWVFUVVVdHV1SWjODAwgG3btsE0TUpfFu3D3d3dME0TO3fuxI4dO9DV1YX9+/ejVCohl8vJMZgYGpUMCaBUPmRw9NyRWiVlEnF/orZRDTAWiyGdTqPdbsNxnDgAdvr06WBkZCS80docqcL0xjVN06ozMzMbMpmMoDMBOge4cOECGo0Gstmsv2vXrpXp6ek8AGGaJtu+fTtmZ2clGDabTbz99tuyXSqKgkKhAMuyUKvV1kx1UfYWPSKnDKE+v7S09FvzRSqVkpyC+Imu67xaraJSqQxpmiZGRkbedVVu/elwljGWWlhYkNFoNBo4c+YMzp49i2vXruGBBx7AXXfd1YrFYucVRbmjUqkIy7LYG2+8IVOS9gaWlpag67oclKh/Rx+EB1FFdz2HaLVasrXGYjHpMJpHUqkUWq2WbMm+7/Pr16+Lq1evDrquO8AYO0/7AO/lgHoqlbKz2Wzy+vXrEs2r1SoSiQQOHDgARVHEW2+9lbhy5crtlmWh3W5z2uiidDVNc83aim3bKJfLa06WKb2jZ480TEXH4qhKRGd/NFqrqiqJERlPOmEymQwWFhbU06dPfwbA+fHxcb5+n1liQKQTNIrF4ktbtmzByspKQGpQPB7HLbfcgr1796Knp4fV63XFsiyFCBBR3nQ6jVwuh1QqJc//V1ZWUK/XZfSjBhNxIZ0hqh2Q8wgMo+oTHZdR3RO/CMMQ9Xodtm3DdV1UKhVRrVZ30eHvjR48snUNALjtttv+euPGjRBC8CAIkE6nkUgkUCwWYZomCoUC8vm8ZIcEQM1mE7VaDUtLS5iZmcH8/Dzm5+dRqVSwsrIi+zv9PV08HYFFxZUoc6QeT2oSdSbHcWTGrF/TWR2lBWOMRZXp99wUHR4e5l//+tfDxx9//OXvfe97++v1epDP55XLly+jv78fiURC1ner1cLqEZPs6YTC0bGUIkyUOLpPSEyRRBbq59EDTyoHOl0iR0WPyohoUbsMgkAEQSC+8IUvhA899NC+zZs3v/ZuGMCj/zh48CAPggC33Xbbt3bs2AHLsuQJTq1Ww9WrVwFAtkIynsCKmF9Uy1uv6qxfiKLP0MZZFASjxlMZkMgSNZz4guM4dJIVfPazn+X33HPPsd9l/I0cEABgg4ODz+7evXuqUCgo9EGKLj1JKDVNU+p7dDFU01SXhAfR9CcgW+XsMprRNbvo39Nnor8jhJBaIGVOEATuQw89pB49evS/3HTTTU+NjY2pv+tmCr5uQVAMDw8rjLH25s2bx/r7+8EYCznnsn3RkRPR36hmQCdI0fE3etHRQSmqEAVBsGZblHCBsoXk+egSpOu6WFlZod0k4ft+UCqVgq985Sv6vffe+9revXv/9fDwsJx13i8PwMGDBzEyMoJ4PD7T3d0tRYhms4lGoyGPraM6PxlBpRCVu6Irc4QVUSWIjCHnRAGSWiAdj5EGsOoo4XleqKqq6OnpUQ8cOKDs27cPAwMDJ7Zs2fIIY8yNboS+bwdECIpHAmM8Hsfy8jIMw0ClUpGRI4EkFotJRI6uqURXZchRtBu4usUp+zmt4K1fsI5mVRAEwnGcEAA6OjqUj3zkI8q+ffuwc+dOq6+v7++7u7sfK5VKY9F7HN5rG/5dHaBpmhk9nCDBInr62mw212xlUvTX3wsUXZB0XReJREKeDUSFDHJMlAh5nids2w6FELxYLLK+vj5laGgI27dvt3fs2PGLrVu3/l1vb+/fMcamqZMdP35cvB/jf6cDAASqqiKZTKJcLstdQZKpoobTIlT0QINSOLoqG90MITGF+naUydHDtu0AgNLf36/s2rULe/bsmdu+ffv/7uvre3b79u0nyWgyfHBwkB09ejQYGRl53/dAvasDEonEXDweR6vVQkdHh0xty7KkyEHpuX5NNroYTRhArYyc2m635bICjdMEmkEQhK1Wi/f29iq333679/GPf/z7hw8ffrJQKJyhu8QIxMfGxvj4+PjvfbfYuzpgaWlJAEAmk7mwWv+cFhdo15+o7fopLrr3s/40iPg8ZQyVV5TNMcbgOE6QzWaVO++8M7zjjjue+tznPvdN0zTPRCN98OBBafShQ4c+kOHv6oBz584JANi9e/f5n//857Zt24kwDIXjOKzVakk+H70HgIxdf3BB/09TJYFno9GAoiiwbVt2DiEEGo2Gv23bNvXBBx88/6UvfemLiUTi1Be/+EUcOXJEOXLkCI4cORIyxj5wtH+fm6aYEAJPPvnkqz/96U8/Wq/XBWNMWVlZkZI1bYmRM6jOyRhaX6GMoNXa6K0w1Epd1xWO44j9+/fz+++//38eO3bsnzPGqnfeeac6Pj4eftC7Qt/Pg9/oxbGxMYUxJrZt2/bf9+zZw8Mw9F3XFclkcs3aS3Q9lgwj8KM7S+j4jPb2HMdBq9USjuOEtm37zWYzSCQS7P777+df+9rX/s3DDz98H2OsOjo6qkxMTPh/TOPfNQOEEPR6+gc/+MErL7744sDZs2eF7/shAG5ZFiNeHlVq15/+rpIjEf6mVsRqrXMhBDdNExs3bkRfXx/27du3dO+99/7L3t7eZwAoQojw/baxP/Rxwy7wG7lPMMZYrV6v38kY+3e5XO6Rc+fOqZcuXYJlWeFq9Fl0I0xRFBEEgVhdeGSMMa6qKjMMQ0kmk8jlcujq6kKxWAy2bNlyZceOHc/t3bv3+W3btp1mjC2RYhtthX/sB3s/d4yuavcfee655/7V6dOn/+nly5c7Z2Zm5GkQPeLxuDyWoqEpk8lcLRaL5zds2HCur6/v/wwMDFzctWvXNQAzjDE38lv8j53uvy8Rkplw9OhR3tnZeQbAg0KITadOnfrE66+//ieLi4u32ratCyEajLEgl8vNdnd3v14qlS7H4/FKT0/P1d7e3jOapjWip0LRW3QGBwcFoTv+Pzz+L1NsvBxy7qHaAAAAAElFTkSuQmCC" width="26" height="26" style="border-radius:5px;display:block">'
    lock_svg = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>'
    return HTMLResponse(f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign in · MailChad</title>
<script>(function(){{try{{var t=localStorage.getItem('mc_theme')||'light';document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}}})();</script>
<style>
:root{{--font-sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--bg:oklch(0.984 0.002 285);--surface:oklch(1 0 0);--surface-2:oklch(0.975 0.003 285);--border:oklch(0.918 0.004 285);--border-2:oklch(0.862 0.006 285);--text:oklch(0.262 0.012 285);--text-muted:oklch(0.505 0.012 285);--text-dim:oklch(0.62 0.01 285);--accent:oklch(0.55 0.205 286);--accent-hover:oklch(0.48 0.205 286);--accent-press:oklch(0.43 0.2 286);--accent-soft:oklch(0.955 0.032 286);--accent-soft-2:oklch(0.925 0.052 286);--accent-border:oklch(0.86 0.07 286);--on-accent:oklch(0.99 0.005 286);--crit-fg:oklch(0.5 0.18 27);--crit-bg:oklch(0.955 0.045 27);--crit-bd:oklch(0.87 0.08 27);--shadow-sm:0 1px 2px oklch(0.2 0.02 285/0.06),0 1px 1px oklch(0.2 0.02 285/0.04);--shadow-lg:0 16px 48px oklch(0.2 0.02 285/0.18),0 4px 12px oklch(0.2 0.02 285/0.1);--ring:0 0 0 3px var(--accent-soft-2);--r-sm:6px;--r-lg:12px;--r-xl:16px;color-scheme:light}}
[data-theme="dark"]{{color-scheme:dark;--bg:oklch(0.178 0.008 285);--surface:oklch(0.212 0.009 285);--surface-2:oklch(0.246 0.011 285);--border:oklch(0.305 0.012 285);--border-2:oklch(0.38 0.016 285);--text:oklch(0.945 0.004 285);--text-muted:oklch(0.71 0.012 285);--text-dim:oklch(0.575 0.012 285);--accent:oklch(0.7 0.16 286);--accent-hover:oklch(0.77 0.15 286);--accent-press:oklch(0.82 0.13 286);--accent-soft:oklch(0.3 0.05 286);--accent-soft-2:oklch(0.36 0.07 286);--on-accent:oklch(0.16 0.02 286);--crit-fg:oklch(0.8 0.13 27);--crit-bg:oklch(0.34 0.08 27);--crit-bd:oklch(0.46 0.1 27);--shadow-lg:0 20px 56px oklch(0 0 0/0.6),0 6px 16px oklch(0 0 0/0.4)}}
*,*::before,*::after{{box-sizing:border-box}}*{{margin:0}}body{{font-family:var(--font-sans);background:var(--bg);color:var(--text);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}}
.login-wrap{{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(120% 120% at 50% 0%,var(--accent-soft) 0%,var(--bg) 45%)}}
.login-card{{width:100%;max-width:384px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);box-shadow:var(--shadow-lg);padding:32px}}
.mark{{width:34px;height:34px;border-radius:9px;background:linear-gradient(150deg,var(--accent),var(--accent-press));display:grid;place-items:center;color:var(--on-accent);box-shadow:var(--shadow-sm)}}
.field{{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}}label{{font-size:12.5px;font-weight:550;color:var(--text)}}
input{{width:100%;background:var(--surface);color:var(--text);border:1px solid var(--border-2);border-radius:var(--r-sm);padding:8px 11px;font-size:13.5px;font-family:inherit;transition:border-color .12s,box-shadow .12s}}input::placeholder{{color:var(--text-dim)}}
input:focus{{outline:none;border-color:var(--accent);box-shadow:var(--ring)}}
.btn{{display:flex;align-items:center;justify-content:center;width:100%;padding:9px 14px;border-radius:var(--r-sm);border:none;font:inherit;font-size:13px;font-weight:550;cursor:pointer;background:var(--accent);color:var(--on-accent);box-shadow:var(--shadow-sm);margin-top:6px;transition:background .12s}}
.btn:hover{{background:var(--accent-hover)}}.btn:active{{background:var(--accent-press)}}
.icon-btn{{appearance:none;width:32px;height:32px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--surface);display:grid;place-items:center;cursor:pointer;color:var(--text-muted)}}
.callout.crit{{display:flex;gap:11px;padding:12px 15px;border-radius:var(--r-sm);background:var(--crit-bg);border:1px solid var(--crit-bd);color:var(--crit-fg);font-size:13px}}
.ct-body{{min-width:0}}
.dim{{color:var(--text-dim);font-size:12px}}
</style>
</head>
<body>
<button class="icon-btn" id="themeToggle" style="position:fixed;top:18px;right:18px" title="Toggle theme" aria-label="Toggle theme"></button>
<div class="login-wrap">
  <div class="login-card">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:22px">
      <span class="mark">{mail_svg}</span>
      <div>
        <div style="font-weight:650;font-size:17px;letter-spacing:-0.03em">MailChad</div>
        <div class="dim">ep-v3 terminal</div>
      </div>
    </div>
    <h1 style="font-size:19px;letter-spacing:-0.025em;margin-bottom:4px">Sign in to your vault</h1>
    <p style="color:var(--text-muted);font-size:13.5px;margin-bottom:20px">All plaintext data stays on this terminal. Authenticate to manage your sending vault.</p>
    {err_html}
    <form method="post" action="/admin/auth/login">
      <div class="field"><label for="email">Email</label><input type="text" id="email" name="email" autofocus autocomplete="username"></div>
      <div class="field"><label for="password">Password</label><input type="password" id="password" name="password" placeholder="••••••••••••" autocomplete="current-password"></div>
      <button class="btn" type="submit">Sign in</button>
    </form>
    <div style="display:flex;align-items:center;gap:7px;margin-top:18px;justify-content:center;color:var(--text-dim);font-size:12px">
      {lock_svg} Session secured with JWT · bcrypt · AES-256-GCM at rest
    </div>
  </div>
</div>
<script>(function(){{
  var sun='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
  var moon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';
  function apply(t){{var r=document.documentElement;r.classList.add('theme-switching');r.setAttribute('data-theme',t);requestAnimationFrame(function(){{requestAnimationFrame(function(){{r.classList.remove('theme-switching');}});}});try{{localStorage.setItem('mc_theme',t);}}catch(e){{}}var btn=document.getElementById('themeToggle');if(btn)btn.innerHTML=t==='dark'?sun:moon;}}
  var btn=document.getElementById('themeToggle');var cur=document.documentElement.getAttribute('data-theme')||'light';if(btn){{btn.innerHTML=cur==='dark'?sun:moon;btn.addEventListener('click',function(){{apply(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark');}});}}
}})();</script>
</body>
</html>""")


@router.get("/admin/auth/login", response_class=HTMLResponse)
def login_page_get(error: str | None = None):
    return login_page(error)


def _check_operator(email: str, password: str) -> dict | None:
    """Returns operator row dict if credentials match, else None."""
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT id, email, password_hash, role, active FROM operators WHERE email=?",
                (email.strip().lower(),),
            ).fetchone()
        if not row:
            return None
        if not row["active"]:
            return None
        if bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            return dict(row)
    except Exception as e:
        log.warning("operator check error: %s", e)
    return None


def _update_last_login(operator_id: int) -> None:
    try:
        with db.conn() as c:
            c.execute("UPDATE operators SET last_login_at=datetime('now') WHERE id=?", (operator_id,))
            c.commit()
    except Exception:
        pass


@router.post("/admin/auth/login")
def login(response: Response, email: str = Form(...), password: str = Form(...)):
    jwt_secret = _jwt_secret()
    ttl_s      = _session_ttl_s()
    if not jwt_secret:
        raise HTTPException(503, "jwt_secret not set - configure via admin UI")

    # Try operators table first
    op = _check_operator(email, password)
    if op:
        _update_last_login(op["id"])
        token = _sign({"sub": op["email"], "role": op["role"],
                       "iat": int(time.time()), "exp": int(time.time()) + ttl_s})
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=False, max_age=ttl_s, path="/")
        return resp

    # Legacy fallback: single admin from settings
    admin_email = _admin_email()
    admin_hash  = _admin_password_hash()
    if not (admin_email and admin_hash):
        return RedirectResponse("/admin/auth/login?error=Invalid+credentials", status_code=303)
    if email.strip().lower() != admin_email.strip().lower():
        return RedirectResponse("/admin/auth/login?error=Invalid+credentials", status_code=303)
    try:
        ok = bcrypt.checkpw(password.encode(), admin_hash.encode())
    except Exception as e:
        log.warning("bcrypt error: %s", e)
        return RedirectResponse("/admin/auth/login?error=Invalid+credentials", status_code=303)
    if not ok:
        return RedirectResponse("/admin/auth/login?error=Invalid+credentials", status_code=303)

    token = _sign({"sub": email, "role": "admin",
                   "iat": int(time.time()), "exp": int(time.time()) + ttl_s})
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=False, max_age=ttl_s, path="/")
    return resp


@router.post("/admin/auth/logout")
def logout(response: Response):
    response = RedirectResponse("/admin/auth/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
