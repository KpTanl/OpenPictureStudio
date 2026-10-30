# AI Services and Privacy

[简体中文](ai-services.md) | [English](ai-services.en.md) | [Français](ai-services.fr.md)

[Back to README](../README.en.md) · [Documentation](README.en.md)

## Local AI Tools and the Retouching Assistant

| Capability | How it works |
| --- | --- |
| Local AI tools such as super resolution, denoise, cutout, and portrait retouching | Prepare the models you need in Settings → Tools and Models. Photos are processed on your computer. The optional GPU runtime is managed in Settings → Runtimes |
| AI retouching assistant | Configure a connection and models in Settings → AI Services. The text, parameters, and photo content you choose are sent to that service, and you get back chat replies, plans, or tool-call results |

Model downloads and service connections are managed separately. Local tools work offline once their models and runtime are ready. The online assistant needs access to the service you chose.

## Setting Up a Service Connection

1. Open Settings → AI Services and click Add connection.
2. Enter the connection name, the base address given by the service provider, and the API key if one is needed. The base address can be the service root, or a full Chat Completions or Responses endpoint address.
3. Under Connection options, choose the API protocol and authentication that the service requires. If you need a proxy or extra request headers, fill those in as well.
4. Click Verify & fetch models, then add models from the returned list. If the service does not return a list, use Add model manually and enter the exact model ID.
5. Expand Model settings and configure options such as image input, tool calling, streaming, and the output limit, according to what the model actually supports.
6. Run a model test to confirm the model replies. You can set a frequently used model as the default, then go back to the assistant panel and start using it.

A model test sends one short test message. API usage, cost, and quota from tests and everyday chats are counted against the service account you chose. A multi-step Agent task can involve several rounds of requests.

## Choosing Model Capabilities by Use

| Use case | Capability to set up |
| --- | --- |
| Text chat and photography advice | A working text model. If it does not support images, choose "Don't send the photo" |
| Visual analysis of the current photo, a reference image, or a plan | A model that supports image input, with Image input turned on in Model settings |
| Agent calling editing tools | A model that supports tool calling, with Tool calling turned on in Model settings |
| High-resolution local detail | Image input is on, and the image options, such as Effective image long edge, are set to match the model |

What a model can do depends on what the service actually supports. Image input, tool calling, and streaming can each be configured separately.

## Using the Assistant

| Mode | Suited for |
| --- | --- |
| Chat | Talking about photography, the picture, and ideas for processing |
| Plan | Checking the current photo and its editing state, and discussing a plan, while leaving the photo unchanged |
| Adjust | Generating and applying color or retouching adjustments that fit your request |
| Agent | Organizing multi-step tool calls around a goal, processing the photo, and checking the result |

Edits the assistant applies successfully are written to the photo's history. You can undo them, redo them, or keep editing by hand. When you want two options, ask for A/B candidates. Candidates are first shown for preview and comparison, and nothing is written to the current edit until you click Adopt.

You can add a reference image to help describe your goal, and you can ask the assistant to save a named style or a long-term preference. Saving a style or preference needs your confirmation on a confirmation card.

## Photo Sending Options

Before you send a message, you can choose how photos are sent below the input box. The top of the assistant panel shows what may be sent for this request.

| Option | Photo content sent |
| --- | --- |
| Current | By default the first message includes a preview of the current photo with your adjustments, with a long edge of up to 1536 pixels |
| Original | The first message includes the image without edit adjustments, with a long edge of up to 2048 pixels |
| Don't send the photo | No photo is attached to the request. Reference images and photos returned by tool observations are not sent either |

An image smaller than these sizes is sent at its original size and is not enlarged. Photos are also not sent when Image input is not turned on for the model.

With photo sending on, the assistant can also look at the original image, the image from before it started, and a preview of the current image when needed, as well as high-resolution local detail that the model settings allow. After you add a reference image, whether it is attached depends on the photo sending option and the model's image capability. Every photo sent is a re-encoded JPEG and does not include the original file's shooting information.

## Text, Parameters, and Photo Analysis

Besides photos, the assistant can send the following, depending on the context options:

- The text you type, plus the selected chat history, standing requirements, and the chat summary.
- The current adjustment parameters, a workflow summary, and relevant tool results from processing tasks.
- Photo analysis computed on your computer, such as brightness, color cast, and faces. This is sent as text or numbers.
- The named style you selected, long-term preferences you confirmed, and the relevant analysis of a reference image.

With "Don't send the photo" selected, the text, parameters, and analysis you allowed can still be part of the request. You can adjust the scope of what is sent in the assistant's context options.

## Local Data and Server-Side Data

API keys, extra request headers, and proxy credentials are encrypted with Windows DPAPI and stored on your computer. Chats, photo preview attachments, named styles, and preferences are also stored on your computer. Whatever you select as request context is sent to the service you chose.

You can delete chats you no longer need, or clear the photo attachments of a chat. Clearing attachments keeps the original photos, the messages, and the photo's edit history. If you need to view a photo later, you can attach the current photo again.

How the server stores and uses data depends on the service provider you chose. Check the settings and privacy policy of your service account to choose a service and a sending scope that suit you.

## Common Connection Problems

| Problem | What to do |
| --- | --- |
| The connection cannot be verified, or authentication fails | Check the base address, API protocol, authentication, key, and account permissions. If you use a proxy, check the proxy address |
| The service returns no model list | Add the model manually, using the model ID given by the service provider |
| A message says the model does not support images | Choose a model that supports images and turn on Image input, or change the photo option to "Don't send the photo" |
| The Agent can only chat in text | Choose a model that supports tool calling, and turn on Tool calling in Model settings |
| The request times out or a streaming reply fails | Check the network and the service status. Adjust the request timeouts to what the service supports, or turn off streaming and try again |
| A message says the context exceeds the budget | Use Compact context, reduce the sending scope, or adjust the context window setting to the model's actual capability |
| A message says attachment space is low | Delete chats you no longer need or clear photo attachments, then attach the photos you need again |

To report a problem, you can use [GitHub Issues](https://github.com/KpTanl/OpenPictureStudio/issues) to provide the app version, the error message, and the steps to reproduce. Before making attachments public, remove information such as API keys, proxy credentials, and private photos.
