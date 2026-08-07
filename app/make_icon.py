#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Zectrix 同步.app 的图标（一张圆角便利贴 + emoji）。

单独拆出来是为了让 make_app.sh 在没有 pyobjc 的机器上也能继续跑完，
只是图标退化成 AppleScript 默认的那个。
"""
import os
import sys

from AppKit import (NSBezierPath, NSBitmapImageRep, NSColor, NSFont,
                    NSGraphicsContext, NSImage, NSMakeRect, NSPNGFileType)
from Foundation import NSMutableDictionary


def draw(size: int) -> bytes:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(
        NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))

    inset = size * 0.06
    rect = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
    card = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, size * 0.22, size * 0.22)

    # 墨水屏的感觉：接近纸白的底 + 深灰边
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.98, 0.97, 0.94, 1.0).set()
    card.fill()
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.16, 0.16, 0.18, 1.0).set()
    card.setLineWidth_(size * 0.035)
    card.stroke()

    emoji = "🔄"
    attrs = NSMutableDictionary.dictionary()
    attrs["NSFont"] = NSFont.fontWithName_size_("Apple Color Emoji", size * 0.46) \
        or NSFont.systemFontOfSize_(size * 0.46)
    text = NSImage.alloc().init()  # 占位，避免 lint 抱怨未使用
    del text

    from Foundation import NSString
    ns = NSString.stringWithString_(emoji)
    text_size = ns.sizeWithAttributes_(attrs)
    ns.drawAtPoint_withAttributes_(
        ((size - text_size.width) / 2, (size - text_size.height) / 2 + size * 0.02),
        attrs)

    NSGraphicsContext.restoreGraphicsState()
    return rep.representationUsingType_properties_(NSPNGFileType, None)


def main(iconset_dir: str) -> int:
    os.makedirs(iconset_dir, exist_ok=True)
    # icns 需要的标准尺寸组合
    for size, names in {
        16: ["icon_16x16.png"],
        32: ["icon_16x16@2x.png", "icon_32x32.png"],
        64: ["icon_32x32@2x.png"],
        128: ["icon_128x128.png"],
        256: ["icon_128x128@2x.png", "icon_256x256.png"],
        512: ["icon_256x256@2x.png", "icon_512x512.png"],
        1024: ["icon_512x512@2x.png"],
    }.items():
        data = draw(size)
        for name in names:
            with open(os.path.join(iconset_dir, name), "wb") as fh:
                fh.write(bytes(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
