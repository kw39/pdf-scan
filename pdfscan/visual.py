"""Local visual proposals. Reference images and QR contents never leave the device."""
import io
import warnings
import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image, ImageOps

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_LOGO_PIXELS = 16_000_000

def logo_png(data):
    if not data or len(data)>MAX_LOGO_BYTES:
        raise ValueError("Choose a PNG, JPEG or WebP logo up to 5 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in ("PNG","JPEG","WEBP") or source.width*source.height>MAX_LOGO_PIXELS:
                    raise ValueError("Use a PNG, JPEG or WebP image up to 16 megapixels.")
                source.load()
                rgba=ImageOps.exif_transpose(source).convert("RGBA")
                image=Image.new("RGB",rgba.size,"white")
                image.paste(rgba,mask=rgba.getchannel("A"))
        gray=np.asarray(image.convert("L"))
        ys,xs=np.where(gray<245)
        if not len(xs): raise ValueError("The reference logo is blank.")
        image=image.crop((int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1))
        image.thumbnail((1024,1024))
        if min(image.size)<8 or np.asarray(image.convert("L")).std()<8:
            raise ValueError("Choose a clear logo with visible detail, at least 8 pixels wide and tall.")
        out=io.BytesIO();image.save(out,"PNG")
        return out.getvalue()
    except (ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(str(exc)) from exc
    except Exception as exc:
        raise ValueError("This logo image could not be read. Use PNG, JPEG or WebP.") from exc

def rendered(page,side=1800):
    scale=min(2,side/max(page.rect.width,page.rect.height))
    pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),colorspace=fitz.csGRAY,alpha=False)
    return np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width)

def bounds(points,width,height,pad=0):
    points=np.asarray(points).reshape(-1,2)
    return [max(0,float(points[:,0].min())/width-pad),max(0,float(points[:,1].min())/height-pad),
            min(1,float(points[:,0].max())/width+pad),min(1,float(points[:,1].max())/height+pad)]

def qr_regions(gray):
    height,width=gray.shape
    detector=cv2.QRCodeDetector()
    found=[]
    # Detection uses finder patterns; no payload decoding or URL navigation.
    for image in (gray,cv2.threshold(gray,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]):
        ok,points=detector.detectMulti(image)
        if ok and points is not None:
            found.extend(bounds(poly,width,height,.006) for poly in points)
        ok,points=detector.detect(image)
        if ok and points is not None:
            found.append(bounds(points,width,height,.006))
    return found

def reference_regions(gray,reference):
    reference=np.asarray(Image.open(io.BytesIO(reference)).convert("L"))
    height,width=gray.shape
    shrink=min(1,1200/max(width,height))
    image=cv2.resize(gray,None,fx=shrink,fy=shrink,interpolation=cv2.INTER_AREA)
    h,w=image.shape
    found=[]
    # Multi-scale, four right-angle orientations. Keep the score for human review.
    sizes=[];size=24.
    while size<=min(420,max(w,h)*.45):
        sizes.append(size);size*=1.05
    for turns in range(4):
        ref=np.rot90(reference,turns).copy()
        for size in sizes:
            factor=size/max(ref.shape)
            tw,th=max(8,round(ref.shape[1]*factor)),max(8,round(ref.shape[0]*factor))
            if tw>=w or th>=h:continue
            template=cv2.resize(ref,(tw,th),interpolation=cv2.INTER_AREA if factor<1 else cv2.INTER_CUBIC)
            if template.std()<8:continue
            scores=cv2.matchTemplate(image,template,cv2.TM_CCOEFF_NORMED)
            for _ in range(12):
                _,score,_,location=cv2.minMaxLoc(scores)
                if score<.87:break
                x,y=location
                rect=[max(0,(x-2)/w),max(0,(y-2)/h),min(1,(x+tw+2)/w),min(1,(y+th+2)/h)]
                found.append((rect,float(score)))
                scores[max(0,y-th//2):min(scores.shape[0],y+th//2+1),max(0,x-tw//2):min(scores.shape[1],x+tw//2+1)]=-1
    return sorted(found,key=lambda item:item[1],reverse=True)

def possible_logos(gray,text_rects):
    h,w=gray.shape
    ink=cv2.threshold(gray,190,255,cv2.THRESH_BINARY_INV)[1]
    ink=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8))
    contours,_=cv2.findContours(ink,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    found=[]
    for contour in contours:
        x,y,cw,ch=cv2.boundingRect(contour)
        if not (.02*w<cw<.3*w and .015*h<ch<.18*h and .35<cw/ch<3):continue
        if not (y+ch<h*.25 or y>h*.75):continue
        rect=[x/w,y/h,(x+cw)/w,(y+ch)/h]
        area=(rect[2]-rect[0])*(rect[3]-rect[1])
        covered=0
        for text in text_rects:
            covered+=max(0,min(rect[2],text[2])-max(rect[0],text[0]))*max(0,min(rect[3],text[3])-max(rect[1],text[1]))
        if covered/area>.25:continue
        if cv2.contourArea(contour)<cw*ch*.08:continue
        found.append(rect)
    return found[:100]
