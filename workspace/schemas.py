from pydantic import BaseModel, EmailStr
from typing import Optional

class StudentBase(BaseModel):
    name: str
    age: int
    email: EmailStr

class StudentCreate(StudentBase):
    pass

class StudentUpdate(StudentBase):
    name: Optional[str] = None
    age: Optional[int] = None
    email: Optional[EmailStr] = None

class Student(StudentBase):
    id: int

    class Config:
        orm_mode = True
