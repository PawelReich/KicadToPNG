import os
import sys
import re
import subprocess
import argparse
import shutil
import glob
import xml.etree.ElementTree as ET
from copy import deepcopy
import cairosvg

class SExprParser:
    def parse(self, content):
        token_re = re.compile(r"""\s*(?:(\()|(\))|("(?:\\[\s\S]|[^"\\])*")|([^\s()]+))""")
        stack = [[]]
        for match in token_re.finditer(content):
            open_p, close_p, string, atom = match.groups()
            if open_p:
                stack.append([])
            elif close_p:
                if len(stack) > 1:
                    completed = stack.pop()
                    stack[-1].append(completed)
                else:
                    raise ValueError("Unbalanced parentheses in file.")
            elif string:
                stack[-1].append(string[1:-1].replace(r'\"', '"'))
            elif atom:
                stack[-1].append(atom)
        return stack[0][0] if stack[0] else None

def extract_textboxes(sexpr):
    textboxes = []
    if not sexpr or sexpr[0] != 'kicad_sch':
        return []

    for item in sexpr[1:]:
        if isinstance(item, list) and item and item[0] == 'text_box':
            text_content = item[1]
            at_node = next((x for x in item if isinstance(x, list) and x[0] == 'at'), None)
            if not at_node: continue
            x, y = float(at_node[1]), float(at_node[2])
            
            size_node = next((x for x in item if isinstance(x, list) and x[0] == 'size'), None)
            if not size_node: continue
            w, h = float(size_node[1]), float(size_node[2])

            effects_node = next((x for x in item if isinstance(x, list) and x[0] == 'effects'), None)
            justify_node = None
            if effects_node:
                justify_node = next((x for x in effects_node if isinstance(x, list) and x[0] == 'justify'), None)
            
            anchor_x, anchor_y = x, y
            is_left = False
            is_top = False
            if justify_node:
                if 'left' in justify_node: is_left = True
                if 'top' in justify_node: is_top = True
            
            final_x = anchor_x if is_left else anchor_x - (w / 2)
            final_y = anchor_y if is_top else anchor_y - (h / 2)

            textboxes.append({'text': text_content, 'x': final_x, 'y': final_y, 'w': w, 'h': h})
            
    return textboxes

def remove_textboxes_raw(content):
    out = []
    i = 0
    n = len(content)
    while i < n:
        if content[i:].startswith('(text_box'):
            balance = 1
            j = i + 1
            in_string = False
            escape = False
            while j < n and balance > 0:
                char = content[j]
                if in_string:
                    if escape: escape = False
                    elif char == '\\': escape = True
                    elif char == '"': in_string = False
                else:
                    if char == '"': in_string = True
                    elif char == '(': balance += 1
                    elif char == ')': balance -= 1
                j += 1
            i = j
        else:
            out.append(content[i])
            i += 1
    return "".join(out)

def export_kicad_to_svg(input_path, output_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    cmd = ["kicad-cli", "sch", "export", "svg", "-n", "--output", output_dir, input_path]
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    svg_files = glob.glob(os.path.join(output_dir, "*.svg"))
    if not svg_files: raise FileNotFoundError("No SVG file generated.")
    return svg_files[0]

def get_svg_scale(root):
    width_str = root.attrib.get('width', '')
    viewbox_str = root.attrib.get('viewBox', '')
    if not width_str or not viewbox_str: return 1.0
    vb_parts = [float(x) for x in viewbox_str.split()]
    if 'mm' in width_str:
        phys_width_mm = float(width_str.replace('mm', ''))
    else:
        return 1.0
    return vb_parts[2] / phys_width_mm

def export_cropped_pngs(source_tree, textboxes, output_dir, scale):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    ET.register_namespace('', "http://www.w3.org/2000/svg")

    for box in textboxes:
        vx = box['x'] * scale
        vy = box['y'] * scale
        vw = box['w'] * scale
        vh = box['h'] * scale
        
        new_tree = deepcopy(source_tree)
        root = new_tree.getroot()
        
        root.attrib['viewBox'] = f"{vx} {vy} {vw} {vh}"
        root.attrib['width'] = f"{box['w']}mm"
        root.attrib['height'] = f"{box['h']}mm"
        
        xml_string = ET.tostring(root, encoding='utf-8')
        
        filename = f"{box['text']}.png"
        file_path = os.path.join(output_dir, filename)
        
        # scale=4 gives roughly 384 DPI (96 * 4)
        cairosvg.svg2png(bytestring=xml_string, write_to=file_path, scale=4.0)
        print(f"Generated PNG: {file_path}")

def main():
    parser = argparse.ArgumentParser(description="Small tool for exporting KiCad sections as graphics")
    parser.add_argument("input_file", help="Path to the .kicad_sch file")
    parser.add_argument("-o", "--output", help="Directory to save PNG files", default=None)
    args = parser.parse_args()

    input_file = args.input_file
    if not os.path.exists(input_file):
        print(f"Error: File '{input_file}' not found.")
        sys.exit(1)

    base_name = os.path.splitext(os.path.basename(input_file))[0]
    output_dir = args.output if args.output else f"{base_name}_pngs"
    temp_dir = f"temp_kicad_export_{base_name}"
    clean_sch_file = os.path.join(os.path.dirname(input_file), f"{base_name}_clean_temp.kicad_sch")

    print(f"Analyzing {input_file}...")
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        sexpr = SExprParser().parse(content)
        textboxes = extract_textboxes(sexpr)
        print(f"   Found {len(textboxes)} text boxes.")
        
        if not textboxes:
            print("No text boxes found. Exiting.")
            sys.exit(0)

        clean_content = remove_textboxes_raw(content)
        with open(clean_sch_file, 'w', encoding='utf-8') as f:
            f.write(clean_content)

    except Exception as e:
        print(f"   Error: {e}")
        if os.path.exists(clean_sch_file): os.remove(clean_sch_file)
        sys.exit(1)

    try:
        svg_file = export_kicad_to_svg(clean_sch_file, temp_dir)
    except Exception as e:
        print(f"   Failed to export: {e}")
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        if os.path.exists(clean_sch_file): os.remove(clean_sch_file)
        sys.exit(1)

    print(f"Generating PNGs")
    try:
        tree = ET.parse(svg_file)
        scale = get_svg_scale(tree.getroot())
        export_cropped_pngs(tree, textboxes, output_dir, scale)
        print(f"   Success! PNGs saved to: {output_dir}/")
        
    except Exception as e:
        print(f"   Error processing images: {e}")
    finally:
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        if os.path.exists(clean_sch_file): os.remove(clean_sch_file)

if __name__ == "__main__":
    main()
