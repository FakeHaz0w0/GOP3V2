/**
 * nsfw_checker.js
 * Called by bot.py as a subprocess:
 *   node nsfw_checker.js <image_path>
 * Outputs a JSON object of nsfwjs predictions to stdout.
 */

const nsfw = require("nsfwjs");
const tf   = require("@tensorflow/tfjs-node");
const fs   = require("fs");
const path = require("path");
const Jimp = require("jimp");

const imagePath = process.argv[2];

if (!imagePath) {
  console.error("Usage: node nsfw_checker.js <image_path>");
  process.exit(1);
}

async function classify(filePath) {
  // Load the model once (cached after first run if you keep the process warm)
  const model = await nsfw.load();

  // Read image with Jimp and convert to a tensor
  const image = await Jimp.read(filePath);
  const { width, height } = image.bitmap;
  const imageData = new Uint8Array(image.bitmap.data);

  // Jimp stores pixels as RGBA; TensorFlow expects RGB
  const rgbData = new Uint8Array(width * height * 3);
  for (let i = 0, j = 0; i < imageData.length; i += 4, j += 3) {
    rgbData[j]     = imageData[i];     // R
    rgbData[j + 1] = imageData[i + 1]; // G
    rgbData[j + 2] = imageData[i + 2]; // B
  }

  const tensor = tf.tensor3d(rgbData, [height, width, 3], "int32");
  const predictions = await model.classify(tensor);
  tensor.dispose();

  // Convert array → object { className: probability }
  const result = {};
  for (const { className, probability } of predictions) {
    result[className] = probability;
  }

  return result;
}

classify(imagePath)
  .then((result) => {
    process.stdout.write(JSON.stringify(result));
    process.exit(0);
  })
  .catch((err) => {
    process.stderr.write(String(err));
    process.exit(1);
  });
