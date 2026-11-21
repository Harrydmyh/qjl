import Foundation
import Vision
import AppKit

for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("\(path)\t-1\t0\t0"); continue
    }
    let req = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform([req])
        let faces = req.results ?? []
        var bestA = 0.0, bestH = 0.0
        for f in faces {
            let b = f.boundingBox
            bestA = max(bestA, Double(b.width) * Double(b.height))
            bestH = max(bestH, Double(b.height))
        }
        print("\(path)\t\(faces.count)\t\(String(format: "%.5f", bestA))\t\(String(format: "%.4f", bestH))")
    } catch {
        print("\(path)\t-2\t0\t0")
    }
}
