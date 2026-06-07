import logging
import random
import string
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dependencies.authz import AnyAuthenticatedUser
from backend.schemas.auth import CurrentUser
from backend.pg import get_pg
from backend.services.email_delivery import send_aws_verification_code_email

log = logging.getLogger("aws_labs")
router = APIRouter(prefix="/aws-labs", tags=["AWS Security Labs"])

AWS_LAB_CONTENT_ID = "c7e66c0d-d421-4f9e-a89c-5b23e7f80da3"


class VerifyCodeRequest(BaseModel):
    code: str


@router.get("/status")
async def get_aws_lab_status(
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    # 1. Query user entitlement for AWS lab
    ent_res = await pg.execute(
        text("""
            SELECT id, valid_until, status
            FROM entitlements
            WHERE user_id = :user_id AND content_id = :content_id
            LIMIT 1
        """),
        {"user_id": current_user.id, "content_id": AWS_LAB_CONTENT_ID}
    )
    ent_row = ent_res.fetchone()

    if not ent_row:
        return {
            "success": True,
            "data": {
                "purchased": False,
                "codeSent": False,
                "codeSentAt": None,
                "codeEntered": False,
                "codeEnteredAt": None,
                "accessGranted": False,
                "subscriptionExpires": None,
                "isExpired": False
            }
        }

    valid_until = ent_row.valid_until
    ent_status = ent_row.status

    # Normalize tz
    now_utc = datetime.now(timezone.utc)
    if valid_until and valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=timezone.utc)

    is_expired = ent_status == "expired" or (valid_until is not None and valid_until < now_utc)

    # 2. Query verification/access state
    ver_res = await pg.execute(
        text("""
            SELECT verification_code, code_sent_at, code_entered, code_entered_at, access_granted, expires_at
            FROM aws_lab_verifications
            WHERE user_id = :user_id
            LIMIT 1
        """),
        {"user_id": current_user.id}
    )
    ver_row = ver_res.fetchone()

    code_sent = False
    code_sent_at = None
    code_entered = False
    code_entered_at = None
    access_granted = False

    if ver_row:
        code_sent = ver_row.verification_code is not None
        code_sent_at = ver_row.code_sent_at
        code_entered = ver_row.code_entered
        code_entered_at = ver_row.code_entered_at
        access_granted = ver_row.access_granted

        if code_sent_at and code_sent_at.tzinfo is None:
            code_sent_at = code_sent_at.replace(tzinfo=timezone.utc)
        if code_entered_at and code_entered_at.tzinfo is None:
            code_entered_at = code_entered_at.replace(tzinfo=timezone.utc)

    return {
        "success": True,
        "data": {
            "purchased": True,
            "codeSent": code_sent,
            "codeSentAt": code_sent_at.isoformat() if code_sent_at else None,
            "codeEntered": code_entered,
            "codeEnteredAt": code_entered_at.isoformat() if code_entered_at else None,
            "accessGranted": access_granted,
            "subscriptionExpires": valid_until.isoformat() if valid_until else None,
            "isExpired": is_expired,
            "purchaseId": str(ent_row.id)
        }
    }


@router.post("/resend-code")
async def resend_aws_access_code(
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    # 1. Verify entitlement
    ent_res = await pg.execute(
        text("""
            SELECT id, valid_until, status
            FROM entitlements
            WHERE user_id = :user_id AND content_id = :content_id
            LIMIT 1
        """),
        {"user_id": current_user.id, "content_id": AWS_LAB_CONTENT_ID}
    )
    ent_row = ent_res.fetchone()

    if not ent_row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You have not purchased AWS Security Labs."
        )

    valid_until = ent_row.valid_until
    ent_status = ent_row.status

    now_utc = datetime.now(timezone.utc)
    if valid_until and valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=timezone.utc)

    if ent_status == "expired" or (valid_until is not None and valid_until < now_utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your AWS Security Labs access has expired."
        )

    # 2. Generate random 6-character alphanumeric code
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    expires_at = now_utc + timedelta(minutes=5)

    # 3. Update or Insert verification row
    await pg.execute(
        text("""
            INSERT INTO aws_lab_verifications (
                user_id, verification_code, code_sent_at, code_entered, code_entered_at, access_granted, expires_at, updated_at
            ) VALUES (
                :user_id, :code, :now, false, null, false, :expires, :now
            )
            ON CONFLICT (user_id) DO UPDATE SET
                verification_code = EXCLUDED.verification_code,
                code_sent_at = EXCLUDED.code_sent_at,
                code_entered = EXCLUDED.code_entered,
                code_entered_at = EXCLUDED.code_entered_at,
                access_granted = EXCLUDED.access_granted,
                expires_at = EXCLUDED.expires_at,
                updated_at = EXCLUDED.updated_at
        """),
        {
            "user_id": current_user.id,
            "code": code,
            "now": now_utc,
            "expires": expires_at
        }
    )
    await pg.commit()

    # 4. Dispatch Email
    ok, err_msg = await send_aws_verification_code_email(
        to_addr=current_user.email,
        code=code
    )
    if not ok:
        log.error("Failed to send AWS verification email to %s: %s", current_user.email, err_msg)

    return {
        "success": True,
        "message": "Access code sent successfully."
    }


@router.post("/verify-code")
async def verify_aws_access_code(
    body: VerifyCodeRequest,
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    # 1. Verify entitlement
    ent_res = await pg.execute(
        text("""
            SELECT id, valid_until, status
            FROM entitlements
            WHERE user_id = :user_id AND content_id = :content_id
            LIMIT 1
        """),
        {"user_id": current_user.id, "content_id": AWS_LAB_CONTENT_ID}
    )
    ent_row = ent_res.fetchone()

    if not ent_row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You have not purchased AWS Security Labs."
        )

    valid_until = ent_row.valid_until
    ent_status = ent_row.status

    now_utc = datetime.now(timezone.utc)
    if valid_until and valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=timezone.utc)

    if ent_status == "expired" or (valid_until is not None and valid_until < now_utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your AWS Security Labs access has expired."
        )

    # 2. Check verification code
    ver_res = await pg.execute(
        text("""
            SELECT verification_code, expires_at, access_granted
            FROM aws_lab_verifications
            WHERE user_id = :user_id
            LIMIT 1
        """),
        {"user_id": current_user.id}
    )
    ver_row = ver_res.fetchone()

    if not ver_row or not ver_row.verification_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No verification code has been requested. Please request a code first."
        )

    expires_at = ver_row.expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < now_utc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The access code has expired. Please request a new one."
        )

    input_code = body.code.strip().upper()
    db_code = ver_row.verification_code.strip().upper()

    if input_code != db_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid access code. Please try again."
        )

    # 3. Code matches and is valid! Update verification state
    await pg.execute(
        text("""
            UPDATE aws_lab_verifications
            SET code_entered = true,
                code_entered_at = :now,
                access_granted = true,
                updated_at = :now
            WHERE user_id = :user_id
        """),
        {"user_id": current_user.id, "now": now_utc}
    )
    await pg.commit()

    return {
        "success": True,
        "message": "Access code verified successfully."
    }
