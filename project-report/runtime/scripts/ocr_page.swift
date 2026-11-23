// Local OCR for scanned report pages. Prints one JSON object per image.
import Foundation
import Vision

for name in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: name)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true
    do {
        try VNImageRequestHandler(url: url).perform([request])
        let lines = (request.results ?? []).compactMap { item -> [String: Any]? in
            guard let top = item.topCandidates(1).first else { return nil }
            return [
                "text": top.string,
                "confidence": Double(top.confidence),
                "x": Double(item.boundingBox.minX),
                "y": Double(item.boundingBox.minY),
            ]
        }.sorted {
            let y0 = $0["y"] as! Double
            let y1 = $1["y"] as! Double
            if abs(y0 - y1) > 0.018 { return y0 > y1 }
            return ($0["x"] as! Double) < ($1["x"] as! Double)
        }
        let result: [String: Any] = ["image": name, "lines": lines]
        let data = try JSONSerialization.data(withJSONObject: result, options: [.fragmentsAllowed])
        print(String(data: data, encoding: .utf8)!)
    } catch {
        fputs("OCR failed for \(name): \(error)\n", stderr)
        exit(1)
    }
}
