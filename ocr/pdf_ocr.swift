// Usage: pdf_ocr <file.pdf> [scale]
// Renders each PDF page and runs Apple Vision OCR. Prints JSON lines to stdout:
//   {"type":"progress","page":N,"pages":M}
//   {"type":"page","page":N,"lines":[{"x":..,"y":..,"w":..,"h":..,"t":".."}]}   (normalized, y from top)
import Foundation
import PDFKit
import Vision
import AppKit

func emit(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

let args = CommandLine.arguments
guard args.count >= 2, let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    FileHandle.standardError.write("cannot open pdf\n".data(using: .utf8)!)
    exit(1)
}
let scale = args.count >= 3 ? CGFloat(Double(args[2]) ?? 3) : 3
// Optional column strips (normalized x ranges, comma separated "x0-x1"): each strip is OCR'd on its
// own, which keeps Vision from merging or dropping lines in dense multi-column tables.
// The full page is always OCR'd first (s = 0); strips follow with s = 1...n.
var strips: [(CGFloat, CGFloat)] = [(0, 1)]
if args.count >= 4 {
    strips += args[3].split(separator: ",").compactMap { part in
        let xs = part.split(separator: "-").compactMap { Double($0) }
        return xs.count == 2 ? (CGFloat(xs[0]), CGFloat(xs[1])) : nil
    }
}

for i in 0..<doc.pageCount {
    autoreleasepool {
        emit(["type": "progress", "page": i + 1, "pages": doc.pageCount])
        guard let page = doc.page(at: i) else { return }
        let b = page.bounds(for: .mediaBox)
        let w = Int(b.width * scale), h = Int(b.height * scale)
        let cs = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: cs, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return }
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
        ctx.scaleBy(x: scale, y: scale)
        page.draw(with: .mediaBox, to: ctx)
        guard let cg = ctx.makeImage() else { return }
        var lines: [[String: Any]] = []
        func recognize(_ image: CGImage) -> [VNRecognizedTextObservation] {
            let req = VNRecognizeTextRequest()
            req.recognitionLevel = .accurate
            req.usesLanguageCorrection = false
            req.minimumTextHeight = 0
            try? VNImageRequestHandler(cgImage: image).perform([req])
            return req.results ?? []
        }
        let white = CGColor(red: 1, green: 1, blue: 1, alpha: 1)
        for (si, (x0, x1)) in strips.enumerated() {
            if si == 0 {
                for o in recognize(cg) {
                    guard let c = o.topCandidates(1).first else { continue }
                    let bb = o.boundingBox
                    lines.append(["x": bb.minX, "y": 1 - bb.maxY, "w": bb.width, "h": bb.height, "t": c.string, "s": 0])
                }
                continue
            }
            // Column strips: cropped, padded with white and cut into tiles of a sane aspect ratio.
            // Vision reads narrow centered table columns far better without neighbours/borders in view.
            let pad = 40
            let cx0 = Int(x0 * CGFloat(w)) + 3, cx1 = Int(x1 * CGFloat(w)) - 3
            let cw = cx1 - cx0
            let tileH = max(cw * 2, 300), overlap = 60
            var ty = 0  // top of tile core, in top-down pixel coordinates
            while ty < h {
                let coreTop = ty, coreBot = min(h, ty + tileH)
                let rTop = max(0, coreTop - overlap), rBot = min(h, coreBot + overlap)
                let rh = rBot - rTop
                // CGImage cropping uses top-left origin
                guard let crop = cg.cropping(to: CGRect(x: cx0, y: rTop, width: cw, height: rh)),
                      let c2 = CGContext(data: nil, width: cw + 2 * pad, height: rh + 2 * pad, bitsPerComponent: 8,
                                         bytesPerRow: 0, space: cs, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
                else { ty += tileH; continue }
                c2.setFillColor(white)
                c2.fill(CGRect(x: 0, y: 0, width: c2.width, height: c2.height))
                c2.draw(crop, in: CGRect(x: pad, y: pad, width: cw, height: rh))
                let img = c2.makeImage()!
                let iw = CGFloat(img.width), ih = CGFloat(img.height)
                for o in recognize(img) {
                    guard let c = o.topCandidates(1).first else { continue }
                    let bb = o.boundingBox
                    // pixel coords in the padded tile, top-down
                    let px = bb.minX * iw - CGFloat(pad) + CGFloat(cx0)
                    let pyTop = (1 - bb.maxY) * ih - CGFloat(pad) + CGFloat(rTop)
                    let ph = bb.height * ih
                    let cy = pyTop + ph / 2
                    if cy < CGFloat(coreTop) || cy >= CGFloat(coreBot) { continue }
                    lines.append(["x": px / CGFloat(w), "y": pyTop / CGFloat(h), "w": bb.width * iw / CGFloat(w),
                                  "h": ph / CGFloat(h), "t": c.string, "s": si])
                }
                ty += tileH
            }
        }
        emit(["type": "page", "page": i + 1, "lines": lines])
    }
}
