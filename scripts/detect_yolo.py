import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
import argparse

import cv2
import numpy as np
import torch
from ultralytics import YOLO
from torchvision import transforms
from PIL import Image

from utils.yaml_helper import read_yaml
from utils.function import non_max_suppression
from utils.function import draw_bbox
from utils.detect_css_violations import detect_css_violations
from utils.detect_css_violations import STrack

def inference(
        weights="weights/best_yolo.pt", 
        img_path="sample/1.jpg", 
        class_names=None,
        detect_thresh=0.3,
        device="cpu"
        ):
    print("weights:", weights, "img_path:", img_path, "class_names:", class_names, "detect_thresh:", detect_thresh, "device:", device)
    CLASS_NAMES = class_names
    DETECT_THRESH = detect_thresh
    
    model = YOLO(weights, DETECT_THRESH)
    transform = transforms.Compose([
        transforms.Resize((640, 640)),
        transforms.ToTensor(),  
    ])
    
    # check if img_path is a file path or an image
    if isinstance(img_path, str):
        image = cv2.imread(img_path) # cv2 format
        if image is None:
            raise FileNotFoundError(f"Image file '{img_path}' not found.")
    elif isinstance(img_path, np.ndarray):
        image = img_path
    else:
        raise ValueError("img_path must be a string (file path) or a numpy array (image).")
    
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    pil_image = Image.fromarray(image)
    image_tensor = transform(pil_image).unsqueeze(0).to(device)
    image = cv2.resize(image, (640, 640))
    # load img and perform inference

    #!----- Detection
    with torch.no_grad():
        detect_results = model(image_tensor)

    
    #!------------ Post-processing: filter low score, nms
    boxes = []
    scores = []
    labels = []
    for result in detect_results:
        for box in result.boxes:
            label, conf, bbox = int(box.cls[0]), float(box.conf[0]), box.xyxy.tolist()[0]
            if conf >= DETECT_THRESH:
                boxes.append(bbox)
                scores.append(conf)
                labels.append(label)

    boxes = torch.tensor(boxes, dtype=torch.float16)
    scores = torch.tensor(scores, dtype=torch.float16)
    labels = torch.tensor(labels, dtype=torch.int64)

    # print("Before NMS boxes:", boxes.shape)
    # print("Before NMS labels:", labels.shape)
    # print("Before NMS scores:", scores.shape)
    unique_labels = torch.unique(labels)
    final_boxes = []
    final_scores = []
    final_labels = []
    for label in unique_labels:
        class_mask = labels == label
        class_boxes = boxes[class_mask]
        class_scores = scores[class_mask]

        keep = non_max_suppression(class_boxes, class_scores, iou_threshold=0.5)

        final_boxes.append(class_boxes[keep])
        final_scores.append(class_scores[keep])
        final_labels.append(torch.full((len(keep),), label, dtype=torch.int16))

# Concatenate results
    if final_boxes:
        boxes = torch.cat(final_boxes)
        scores = torch.cat(final_scores)
        labels = torch.cat(final_labels)
    else:
        boxes = torch.empty((0, 4), dtype=torch.float32)
        scores = torch.empty((0,), dtype=torch.float32)
        labels = torch.empty((0,), dtype=torch.int16)  
    # keep = non_max_suppression(boxes, scores, iou_threshold=0.5)
    # keep = non_max_suppression(boxes, scores, labels, iou_threshold=0.8)

    # print("After NMS boxes:", boxes.shape)
    # print("After NMS labels:", labels.shape)
    # print("After NMS scores:", scores.shape)
    #----
    per_detections = []
    obj_detections = []
    image_detection = image.copy()
    for i in range(len(boxes)):
        x1, y1, x2, y2 = map(int, boxes[i])
        class_id = int(labels[i])
        score = float(scores[i])
        # Detections bbox format for tracker
        if CLASS_NAMES[class_id] == "Person": # only track person
            per_detections.append([x1, y1, x2, y2, score])
            
        else:
            obj_detections.append([x1, y1, x2, y2, score, class_id])
        draw_bbox(image_detection, class_id, x1, y1, x2, y2, score, type='detect', class_names=CLASS_NAMES)
    
    ##!----- CSS violation
    online_targets = []
    for i, t in enumerate(per_detections):
        tlwh = [t[0], t[1], t[2]-t[0], t[3]-t[1]] # xyxy to tlwh
        online_targets.append(STrack(tlwh, t[4], i))
    

    ## CSS violation
    online_targets = detect_css_violations(online_targets, obj_detections) #! CSS violation

    image_violate = image.copy()
    for t in online_targets:
        tlwh = t.tlwh
        tid = t.track_id
        print("Missing:", t.missing)
        x1, y1, w, h = map(int, tlwh)
        x2, y2 = x1 + w, y1 + h
        draw_bbox(image_violate, tid, x1, y1, x2, y2, t.score, missing=t.missing, type='violate', class_names=CLASS_NAMES)
    
    return image_detection, image_violate

if __name__ == "__main__":
# Argument parser
    parser = argparse.ArgumentParser(description="Parser for YOLO inference")
    parser.add_argument("--weights", type=str, default="weights/yolo.pt", help="Path to pretrained weights")
    parser.add_argument("--img_path", type=str, default="sample/1.jpg", help="Path to source image")
    parser.add_argument("--yaml_class", type=str, default="data/data-ppe.yaml", help="Path to class yaml file")

    args = parser.parse_args()
    yaml_class = read_yaml(args.yaml_class)
    CLASS_NAMES = yaml_class["names"]
    DETECT_THRESH = 0.3

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    image_detection, image_violate = inference(device=device, weights=args.weights, img_path=args.img_path, class_names=CLASS_NAMES, detect_thresh=DETECT_THRESH)
        
    # Save the image
    output_dir = "output"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    num = 1

    image_detection = cv2.cvtColor(image_detection, cv2.COLOR_RGB2BGR)
    image_violate = cv2.cvtColor(image_violate, cv2.COLOR_RGB2BGR)
    while os.path.exists(os.path.join(output_dir, f"inference-{num}-yolo.jpg")):
        num += 1
    output_path = os.path.join(output_dir, f"inference-{num}-yolo.jpg")
    cv2.imwrite(output_path, image_detection)
    print(f"Saved inference result to {output_path}") 

    output_path = os.path.join(output_dir, f"inference-{num}-yolo-violate.jpg")
    cv2.imwrite(output_path, image_violate)
    print(f"Saved violate detection result to {output_path}") 