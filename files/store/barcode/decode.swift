// 条形码解码验证器 —— 用 macOS 原生 Vision 扫图里的条码
// 用法: swift decode.swift <图片路径>
//
// 为什么用它验证：Vision 是苹果自己的实现（和 iPhone 相机同源）。
// 生成的条码能被它扫出来，说明**手机上真能扫**，不只是"看起来像"。
import Foundation
import Vision
import CoreImage

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("用法: swift decode.swift <图片路径>\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: args[1])
guard let ci = CIImage(contentsOf: url) else {
    print("ERROR 读不到图片")
    exit(1)
}

// 所有一维码 + 常见二维码
let all: [VNBarcodeSymbology] = [
    .code128, .ean13, .ean8, .upce, .code39, .code39Checksum,
    .code93, .code93i, .i2of5, .i2of5Checksum, .itf14, .codabar,
    .gs1DataBar, .gs1DataBarExpanded, .gs1DataBarLimited,
    .qr, .pdf417, .aztec, .dataMatrix, .microQR, .microPDF417,
]

let req = VNDetectBarcodesRequest()
req.symbologies = all
let handler = VNImageRequestHandler(ciImage: ci, options: [:])
do {
    try handler.perform([req])
} catch {
    print("ERROR Vision 失败: \(error)")
    exit(1)
}
let results = req.results ?? []
if results.isEmpty {
    print("NONE")
    exit(0)
}
for r in results {
    let payload = r.payloadStringValue ?? ""
    print("\(r.symbology.rawValue)\t\(payload)")
}
