import io
import pandas as pd
import PyPDF2
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from sqlalchemy.orm import Session
from app.api.deps import require_api_key
from app.database.database import get_db
from app.database.models import BusinessProfile, BusinessDocument

router = APIRouter(prefix="/api/business", tags=["Business Data"])

@router.post("/setup")
async def setup_business_agent(
    owner_id: str = Form(...),
    business_name: str = Form(...),
    business_type: str = Form(...),
    business_rules: str = Form(""),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    _: None = Depends(require_api_key)
):
    # 1. Create or Update the Core Business Profile
    biz_id = f"biz_{owner_id}"
    business = db.query(BusinessProfile).filter(BusinessProfile.id == biz_id).first()
    
    if not business:
        business = BusinessProfile(
            id=biz_id,
            owner_id=owner_id,
            business_name=business_name,
            business_type=business_type,
            rules_and_policies=business_rules
        )
        db.add(business)
    else:
        business.business_name = business_name
        business.business_type = business_type
        business.rules_and_policies = business_rules
    
    db.commit()

    # 2. Universal Data Extraction Pipeline
    extracted_text = ""
    if file:
        content = await file.read()
        try:
            filename_lower = file.filename.lower()
            
            # Extract PDF Text
            if filename_lower.endswith(".pdf"):
                pdf_reader = PyPDF2.PdfReader(io.BytesIO(content))
                extracted_text = "\n".join(page.extract_text() for page in pdf_reader.pages if page.extract_text())
                
            # Extract CSV Tables
            elif filename_lower.endswith(".csv"):
                df = pd.read_csv(io.BytesIO(content))
                extracted_text = df.to_string(index=False) 
                
            # Extract Excel Tables
            elif filename_lower.endswith((".xls", ".xlsx")):
                df = pd.read_excel(io.BytesIO(content))
                extracted_text = df.to_string(index=False)
                
            # Standard Text
            else:
                extracted_text = content.decode('utf-8')
                
            # Burn the extracted knowledge to the database
            doc = BusinessDocument(
                business_id=biz_id,
                filename=file.filename,
                extracted_text=extracted_text
            )
            db.add(doc)
            db.commit()
            
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Universal Data Extraction failed: {str(e)}")

    return {
        "status": "success",
        "message": f"Agent initialized successfully for {business_name}",
        "business_id": biz_id,
        "knowledge_bytes": len(extracted_text.encode('utf-8'))
    }